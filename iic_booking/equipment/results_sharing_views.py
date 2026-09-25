"""API views: results inbox, internal research-data sharing, public equipment availability."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Min, OuterRef, Q, Subquery
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.communication.utils import booking_display_id_for_email
from iic_booking.equipment.api_views import (
    _booking_requires_istem_fbr_results_block,
    _booking_results_gates_apply,
    _user_may_access_booking_results,
    get_visible_equipment_queryset,
)
from iic_booking.equipment.booking_results_service import booking_has_results_annotation
from iic_booking.equipment.models import (
    Booking,
    BookingDataShare,
    BookingResultView,
    BookingStatus,
    DailySlot,
    EquipmentStatus,
    SlotStatus,
)
from iic_booking.equipment.results_sharing_service import (
    active_shares_for_booking,
    booking_input_summary,
    internal_iitr_users,
    is_internal_iitr_user,
    mark_results_viewed,
    notify_share_recipient,
    user_share_details,
    user_share_summary,
)

USER_SEARCH_MIN_CHARS = 3
USER_SEARCH_LIMIT = 10
PUBLIC_AVAILABILITY_MAX_DAYS = 31
PUBLIC_AVAILABILITY_DATES_PER_EQUIPMENT = 5
PUBLIC_AVAILABILITY_CACHE_SECONDS = 60

NOT_INTERNAL_ERROR = "Research data sharing is available only to IIT Roorkee students and faculty."


def _bookings_with_results(queryset):
    return queryset.annotate(has_results_db=booking_has_results_annotation()).filter(
        Q(has_results_db=True) | Q(results_available_notified_at__isnull=False)
    )


def _results_lock(booking) -> tuple[str, str] | tuple[None, None]:
    """Booking-level reason the owner cannot download results yet (code, message)."""
    if booking.status != BookingStatus.COMPLETED:
        return "not_completed", "Results can be downloaded once the booking is completed."
    if getattr(booking.equipment, "user_rating_enabled", True) and (booking.rating is None or booking.rating_removed):
        return "rating_required", "Submit your rating for this booking to unlock the results."
    if _booking_requires_istem_fbr_results_block(booking):
        return "istem_fbr_not_executed", "Your I-STEM FBR must be verified before results can be downloaded."
    return None, None


def _results_available_at(booking):
    candidates = [
        booking.results_available_notified_at,
        getattr(booking, "last_result_file_at", None),
        getattr(booking, "last_dsa_result_at", None),
    ]
    candidates = [c for c in candidates if c]
    return max(candidates) if candidates else (booking.completed_at or booking.updated_at)


def _annotate_results_timing(queryset, user):
    from iic_booking.equipment.models import BookingResultFile
    from iic_booking.sync.models import EquipmentResult

    last_file = BookingResultFile.objects.filter(booking_id=OuterRef("pk")).order_by("-created_at").values("created_at")[:1]
    last_dsa = EquipmentResult.objects.filter(booking_id=OuterRef("pk")).order_by("-created_at").values("created_at")[:1]
    viewed = BookingResultView.objects.filter(booking_id=OuterRef("pk"), user=user).values("last_viewed_at")[:1]
    return queryset.annotate(
        last_result_file_at=Subquery(last_file),
        last_dsa_result_at=Subquery(last_dsa),
        viewed_at=Subquery(viewed),
        first_slot_at=Min("daily_slots__start_datetime"),
    )


def _booking_card(booking) -> dict:
    equipment = booking.equipment
    department = getattr(equipment, "internal_department", None)
    return {
        "booking_id": booking.booking_id,
        "display_id": booking_display_id_for_email(booking),
        "virtual_booking_id": (booking.virtual_booking_id or "").strip() or None,
        "status": booking.status,
        "status_display": booking.get_status_display(),
        "equipment_id": equipment.equipment_id,
        "equipment_code": equipment.code,
        "equipment_name": equipment.name,
        "department_name": department.name if department else None,
        "booking_date": getattr(booking, "first_slot_at", None),
        "completed_at": booking.completed_at,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def results_inbox(request):
    """Requester's bookings that have results: not-yet-downloaded first, each group most recent first."""
    qs = _bookings_with_results(Booking.objects.filter(user=request.user))
    qs = _annotate_results_timing(qs, request.user).select_related("equipment", "equipment__internal_department")
    share_counts = dict(
        BookingDataShare.objects.filter(booking__user=request.user, revoked_at__isnull=True)
        .values("booking_id")
        .annotate(n=Count("id"))
        .values_list("booking_id", "n")
    )
    items = []
    for booking in qs:
        code, message = _results_lock(booking)
        item = _booking_card(booking)
        item.update(
            {
                "results_available_at": _results_available_at(booking),
                "viewed_at": booking.viewed_at,
                "is_new": booking.viewed_at is None,
                "locked_code": code,
                "locked_reason": message,
                "active_share_count": share_counts.get(booking.booking_id, 0),
            }
        )
        items.append(item)
    epoch = timezone.now() - timedelta(days=365 * 50)
    items.sort(key=lambda i: i["results_available_at"] or epoch, reverse=True)
    items.sort(key=lambda i: 0 if i["is_new"] else 1)
    return Response(
        {
            "results": items,
            "count": len(items),
            "new_count": sum(1 for i in items if i["is_new"]),
            "can_share": is_internal_iitr_user(request.user),
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_results_mark_viewed(request, booking_id):
    """Record a results download that bypassed the API (e.g. presigned S3 link)."""
    booking = Booking.objects.select_related("equipment").filter(booking_id=booking_id).first()
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _user_may_access_booking_results(request.user, booking):
        return Response({"error": "You don't have permission to view this booking's results."}, status=status.HTTP_403_FORBIDDEN)
    if _booking_results_gates_apply(request.user, booking):
        code, message = _results_lock(booking)
        if code:
            return Response({"error": message, "code": code}, status=status.HTTP_403_FORBIDDEN)
    mark_results_viewed(request.user, booking)
    view = BookingResultView.objects.filter(booking=booking, user=request.user).first()
    return Response({"booking_id": booking.booking_id, "viewed_at": view.last_viewed_at if view else None})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def data_sharing_user_search(request):
    if not is_internal_iitr_user(request.user):
        return Response({"error": NOT_INTERNAL_ERROR}, status=status.HTTP_403_FORBIDDEN)
    q = (request.query_params.get("q") or "").strip()
    if len(q) < USER_SEARCH_MIN_CHARS:
        return Response({"results": [], "min_chars": USER_SEARCH_MIN_CHARS})
    users = (
        internal_iitr_users()
        .exclude(pk=request.user.pk)
        .filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(emp_id__iexact=q))
        .order_by("name", "email")[:USER_SEARCH_LIMIT]
    )
    return Response({"results": [user_share_summary(u) for u in users], "min_chars": USER_SEARCH_MIN_CHARS})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def data_sharing_user_detail(request, user_id):
    if not is_internal_iitr_user(request.user):
        return Response({"error": NOT_INTERNAL_ERROR}, status=status.HTTP_403_FORBIDDEN)
    target = internal_iitr_users().exclude(pk=request.user.pk).filter(pk=user_id).first()
    if target is None:
        return Response({"error": "User not found or not eligible for data sharing."}, status=status.HTTP_404_NOT_FOUND)
    return Response(user_share_details(target))


def _share_block_reason(booking) -> str | None:
    if booking.status != BookingStatus.COMPLETED:
        return "Only completed bookings can be shared."
    if not _bookings_with_results(Booking.objects.filter(pk=booking.pk)).exists():
        return "This booking has no results to share yet."
    code, _message = _results_lock(booking)
    if code == "rating_required":
        return "Submit your rating for this booking before sharing its results."
    if code == "istem_fbr_not_executed":
        return "The I-STEM FBR must be verified before results can be shared."
    return None


def _share_row(share: BookingDataShare) -> dict:
    return {
        "id": share.pk,
        "shared_with": user_share_details(share.shared_with),
        "created_at": share.created_at,
    }


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_data_shares(request, booking_id):
    """Owner lists (GET) or creates (POST, requires confirm=true) research-data shares for a booking."""
    booking = Booking.objects.select_related("equipment", "user").filter(booking_id=booking_id).first()
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.user_id != request.user.pk:
        return Response({"error": "Only the booking owner can share its data."}, status=status.HTTP_403_FORBIDDEN)

    requester_internal = is_internal_iitr_user(request.user)
    block_reason = None if requester_internal else NOT_INTERNAL_ERROR
    block_reason = block_reason or _share_block_reason(booking)

    if request.method == "GET":
        return Response(
            {
                "booking_id": booking.booking_id,
                "display_id": booking_display_id_for_email(booking),
                "can_share": block_reason is None,
                "reason": block_reason,
                "shares": [_share_row(s) for s in active_shares_for_booking(booking)],
            }
        )

    if block_reason:
        return Response({"error": block_reason}, status=status.HTTP_400_BAD_REQUEST)
    if request.data.get("confirm") is not True:
        return Response(
            {"error": "Please confirm sharing before submitting.", "code": "confirmation_required"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        target_id = int(request.data.get("user_id"))
    except (TypeError, ValueError):
        return Response({"error": "A valid user_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    target = internal_iitr_users().exclude(pk=request.user.pk).filter(pk=target_id).first()
    if target is None:
        return Response({"error": "User not found or not eligible for data sharing."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with transaction.atomic():
            share = BookingDataShare.objects.create(booking=booking, shared_by=request.user, shared_with=target)
    except IntegrityError:
        return Response(
            {"error": "This booking's data is already shared with that user."},
            status=status.HTTP_409_CONFLICT,
        )
    share_id = share.pk
    transaction.on_commit(lambda: _notify_share(share_id))
    return Response(_share_row(share), status=status.HTTP_201_CREATED)


def _notify_share(share_id: int) -> None:
    share = (
        BookingDataShare.objects.select_related("booking", "booking__equipment", "shared_by", "shared_with")
        .filter(pk=share_id, revoked_at__isnull=True)
        .first()
    )
    if share is not None:
        notify_share_recipient(share)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_data_share_revoke(request, booking_id, share_id):
    share = (
        BookingDataShare.objects.select_related("booking")
        .filter(pk=share_id, booking_id=booking_id)
        .first()
    )
    if share is None:
        return Response({"error": "Share not found."}, status=status.HTTP_404_NOT_FOUND)
    if share.booking.user_id != request.user.pk:
        return Response({"error": "Only the booking owner can revoke sharing."}, status=status.HTTP_403_FORBIDDEN)
    if share.revoked_at is None:
        share.revoked_at = timezone.now()
        share.revoked_by = request.user
        share.save(update_fields=["revoked_at", "revoked_by"])
    return Response({"id": share.pk, "revoked_at": share.revoked_at})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def shared_with_me(request):
    """Active research-data shares received by the requester, with booking details."""
    if not is_internal_iitr_user(request.user):
        return Response({"results": [], "count": 0, "eligible": False})
    shares = list(
        BookingDataShare.objects.filter(shared_with=request.user, revoked_at__isnull=True)
        .select_related("shared_by", "shared_by__department", "booking__equipment__internal_department")
        .order_by("-created_at")
    )
    booking_ids = [s.booking_id for s in shares]
    bookings = {
        b.booking_id: b
        for b in _annotate_results_timing(Booking.objects.filter(booking_id__in=booking_ids), request.user)
        .select_related("equipment", "equipment__internal_department")
    }
    items = []
    for share in shares:
        booking = bookings.get(share.booking_id)
        if booking is None:
            continue
        code, _message = _results_lock(booking)
        card = _booking_card(booking)
        card.update(
            {
                "share_id": share.pk,
                "shared_at": share.created_at,
                "shared_by": user_share_summary(share.shared_by),
                "total_time_minutes": booking.total_time_minutes,
                "atmosphere_sensitive_sample": booking.atmosphere_sensitive_sample,
                "inputs": booking_input_summary(booking),
                "results_available_at": _results_available_at(booking),
                "results_accessible": code is None,
                "viewed_at": booking.viewed_at,
                "is_new": booking.viewed_at is None,
            }
        )
        items.append(card)
    return Response({"results": items, "count": len(items), "eligible": True})


@api_view(["GET"])
@permission_classes([AllowAny])
def public_equipment_availability(request):
    """Next available dates per publicly visible equipment. Read-only: never generates slots."""
    try:
        days = int(request.query_params.get("days") or 14)
    except ValueError:
        days = 14
    days = max(1, min(days, PUBLIC_AVAILABILITY_MAX_DAYS))
    search = (request.query_params.get("search") or "").strip()
    department = (request.query_params.get("department") or "").strip()

    cache_key = f"public_equipment_availability:{days}:{department}:{search.lower()}"
    cached = cache.get(cache_key)
    if cached is not None:
        return Response(cached)

    now = timezone.now()
    last_date = timezone.localdate() + timedelta(days=days)
    equipment_qs = (
        get_visible_equipment_queryset(AnonymousUser())
        .filter(status=EquipmentStatus.ACTIVE)
        .select_related("internal_department")
    )
    departments = sorted(
        {
            (e.internal_department_id, e.internal_department.name)
            for e in equipment_qs
            if e.internal_department_id
        },
        key=lambda d: d[1],
    )
    if department.isdigit():
        equipment_qs = equipment_qs.filter(internal_department_id=int(department))
    if search:
        equipment_qs = equipment_qs.filter(Q(name__icontains=search) | Q(code__icontains=search))
    equipment = list(equipment_qs.order_by("name"))

    free_slots = DailySlot.objects.filter(
        slot_master__equipment_id__in=[e.equipment_id for e in equipment],
        slot_master__is_active=True,
        status=SlotStatus.AVAILABLE,
        home_department_only=False,
        start_datetime__gt=now,
        date__lte=last_date,
    )
    per_date: dict[int, list[dict]] = {}
    for row in (
        free_slots.values("slot_master__equipment_id", "date")
        .annotate(n=Count("id"), first=Min("start_datetime"))
        .order_by("slot_master__equipment_id", "date")
    ):
        per_date.setdefault(row["slot_master__equipment_id"], []).append(
            {"date": row["date"], "available_slots": row["n"], "first_slot_at": row["first"]}
        )

    rows = []
    for e in equipment:
        dates = per_date.get(e.equipment_id, [])
        rows.append(
            {
                "equipment_id": e.equipment_id,
                "code": e.code,
                "name": e.name,
                "department_id": e.internal_department_id,
                "department_name": e.internal_department.name if e.internal_department_id else None,
                "next_available_at": dates[0]["first_slot_at"] if dates else None,
                "total_available_slots": sum(d["available_slots"] for d in dates),
                "dates": dates[:PUBLIC_AVAILABILITY_DATES_PER_EQUIPMENT],
            }
        )
    far_future = now + timedelta(days=3650)
    rows.sort(key=lambda r: (r["next_available_at"] is None, r["next_available_at"] or far_future, r["name"]))

    payload = {
        "generated_at": now,
        "days": days,
        "until": last_date,
        "departments": [{"id": d[0], "name": d[1]} for d in departments],
        "equipment": rows,
        "count": len(rows),
    }
    cache.set(cache_key, payload, PUBLIC_AVAILABILITY_CACHE_SECONDS)
    return Response(payload)
