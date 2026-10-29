"""Courier tracking API (mounted at /api/v1/courier-tracking/).

Users: courier list, their booking's arrival (eligibility + declaration), their shipments, rate-limited refresh.
Staff: shipments of equipment in their existing scope (Admin all; OIC / Department Admin / Lab Operator by equipment;
Accounts In Charge: external bookings, as for the external sample workflow). Main Administrator: usage, settings,
alerts. The webhook receiver is unauthenticated and verified by signature. Department enablement itself is managed
in Department Modules (/api/v1/admin/department-modules/).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.csrf import csrf_exempt
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.department_modules import access as module_access
from iic_booking.department_modules import audit as module_audit
from iic_booking.users.models.user_type import UserType

from . import quota, services, webhooks
from .constants import TERMINAL_STATUSES, ArrivalMode, LookupTrigger, ShipmentStatus
from .eligibility import MODULE, arrival_eligibility
from .models import CourierTrackingAlert, CourierTrackingSettings, SampleArrival, TrackingLookup, TrackingWebhookEvent
from .serializers import SETTINGS_FIELDS, ArrivalRequestSerializer, SettingsSerializer, arrival_dict, refusal_message

logger = logging.getLogger(__name__)

STAFF_TYPES = (UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN, UserType.OPERATOR, UserType.FINANCE)


# -- scope -----------------------------------------------------------------------------------------------------------

def _staff_equipment_ids(user):
    """None: everything; list: these equipment; "external": external bookings (Accounts In Charge)."""
    if module_access.is_main_admin(user):
        return None
    if user.user_type == UserType.FINANCE:
        return "external"
    if user.user_type in (UserType.MANAGER, UserType.DEPT_ADMIN, UserType.OPERATOR):
        from iic_booking.equipment.api_views import _get_equipment_ids_for_log_access

        return list(_get_equipment_ids_for_log_access(user) or [])
    return []


def _is_staff(user) -> bool:
    return module_access.is_main_admin(user) or getattr(user, "user_type", None) in STAFF_TYPES


def _staff_queryset(user):
    scope = _staff_equipment_ids(user)
    qs = SampleArrival.objects.all()
    if scope is None:
        return qs
    if scope == "external":
        return qs.filter(booking__user_type_snapshot__in=UserType.get_external_user_codes())
    return qs.filter(equipment_id__in=scope)


def _can_view(user, arrival: SampleArrival) -> bool:
    if arrival.booking.user_id == user.id:
        return True
    return _is_staff(user) and _staff_queryset(user).filter(pk=arrival.pk).exists()


def _booking_for(user, booking_id):
    from iic_booking.equipment.models import Booking

    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), pk=booking_id)
    if booking.user_id == user.id:
        return booking, False
    if _is_staff(user):
        scope = _staff_equipment_ids(user)
        visible = (
            scope is None
            or (scope == "external" and booking.user_type_snapshot in UserType.get_external_user_codes())
            or (isinstance(scope, list) and booking.equipment_id in scope)
        )
        if visible:
            return booking, True
    return None, False


def _base_qs():
    return SampleArrival.objects.select_related("booking__user", "booking__equipment", "equipment", "department")


# -- couriers --------------------------------------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def couriers(request):
    """Supported couriers (cached; refreshed from the provider at most daily, which costs no quota)."""
    items, fresh = services.catalog()
    if not items:
        return Response({"error": "The courier list is not available right now. Please try again.",
                         "code": "catalog_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    response = Response({"couriers": items, "count": len(items), "fresh": fresh})
    response["Cache-Control"] = "private, max-age=3600"
    return response


# -- booking arrival -------------------------------------------------------------------------------------------------

@transaction.non_atomic_requests  # the booking row lock in register_arrival is the transaction that matters
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_arrival(request, booking_id: int):
    booking, staff_view = _booking_for(request.user, booking_id)
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        elig = arrival_eligibility(request.user, booking)
        arrivals = list(_base_qs().filter(booking=booking).order_by("-created_at")[:20])
        active = next((a for a in arrivals if a.is_active), None)
        return Response(
            {
                "booking_id": booking.pk,
                "eligibility": elig.as_dict(),
                "active": arrival_dict(active, staff=staff_view) if active else None,
                "history": [arrival_dict(a, staff=staff_view, include_history=False) for a in arrivals if not a.is_active],
            }
        )
    if staff_view:
        return Response({"error": "Only the person who made the booking can declare how the sample arrives."},
                        status=status.HTTP_403_FORBIDDEN)
    serializer = ArrivalRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"error": "Please check the highlighted fields.", "fields": serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)
    data = serializer.validated_data
    try:
        arrival = services.register_arrival(
            request.user,
            booking,
            services.ArrivalInput(
                arrival_mode=data["arrival_mode"],
                courier_slug=data.get("courier_slug", ""),
                tracking_number=data.get("tracking_number", ""),
                sample_identifiers=data.get("sample_identifiers", ""),
                dispatch_date=data.get("dispatch_date"),
                expected_arrival_date=data.get("expected_arrival_date"),
                notes=data.get("notes", ""),
                email_updates=data.get("email_updates", False),
            ),
        )
    except services.ArrivalError as exc:
        body = {"error": exc.message, "code": exc.code}
        if exc.field:
            body["fields"] = {exc.field: [exc.message]}
        return Response(body, status=exc.status)
    arrival = _base_qs().get(pk=arrival.pk)
    message = (
        "Courier shipment registered. We will check its status shortly; courier updates can take several hours."
        if arrival.arrival_mode == ArrivalMode.COURIER
        else "Manual delivery recorded. Bring the sample to the lab; staff will confirm when they receive it."
    )
    return Response({"message": message, "arrival": arrival_dict(arrival)}, status=status.HTTP_201_CREATED)


# -- shipments -------------------------------------------------------------------------------------------------------

def _apply_filters(qs, params):
    def _int(name):
        try:
            return int(params.get(name)) if params.get(name) not in (None, "") else None
        except (TypeError, ValueError):
            return None

    for field, name in (("department_id", "department"), ("equipment_id", "equipment"), ("booking_id", "booking"),
                        ("booking__user_id", "user")):
        value = _int(name)
        if value is not None:
            qs = qs.filter(**{field: value})
    if params.get("courier"):
        qs = qs.filter(courier_slug=str(params["courier"]).strip().lower())
    if params.get("status"):
        qs = qs.filter(status__in=[s.strip().upper() for s in str(params["status"]).split(",") if s.strip()])
    if params.get("arrival_mode"):
        qs = qs.filter(arrival_mode=str(params["arrival_mode"]).upper())
    if params.get("sample"):
        qs = qs.filter(sample_identifiers__icontains=str(params["sample"])[:100])
    text = str(params.get("q") or "").strip()[:100]
    if text:
        qs = qs.filter(
            Q(equipment__code__icontains=text) | Q(equipment__name__icontains=text)
            | Q(department__name__icontains=text) | Q(booking__virtual_booking_id__icontains=text)
            | Q(booking__user__email__icontains=text) | Q(booking__user__name__icontains=text)
            | Q(tracking_number__icontains=text.replace(" ", "").upper()) | Q(sample_identifiers__icontains=text)
            | Q(courier_name__icontains=text)
        )
    for name, lookup in (("expected_from", "expected_arrival_date__gte"), ("expected_to", "expected_arrival_date__lte")):
        day = parse_date(str(params.get(name) or ""))
        if day:
            qs = qs.filter(**{lookup: day})
    synced_before = parse_date(str(params.get("synced_before") or ""))
    if synced_before:
        qs = qs.filter(Q(last_lookup_success_at__date__lt=synced_before) | Q(last_lookup_success_at__isnull=True))
    if str(params.get("overdue") or "").lower() in ("1", "true", "yes"):
        qs = qs.filter(expected_arrival_date__lt=timezone.localdate(), is_active=True).exclude(
            status__in=TERMINAL_STATUSES
        )
    if str(params.get("include_inactive") or "").lower() not in ("1", "true", "yes"):
        qs = qs.filter(is_active=True)
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def shipments(request):
    user = request.user
    staff = _is_staff(user) and str(request.query_params.get("view") or "") == "staff"
    qs = _staff_queryset(user) if staff else SampleArrival.objects.filter(booking__user=user)
    qs = _apply_filters(qs.select_related("booking__user", "booking__equipment", "equipment", "department"),
                        request.query_params).order_by("-created_at")
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    size = 25
    total = qs.count()
    rows = list(qs[(page - 1) * size: page * size])
    return Response(
        {
            "count": total,
            "page": page,
            "page_size": size,
            "results": [arrival_dict(a, staff=staff, include_history=False) for a in rows],
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def shipment_detail(request, pk: int):
    arrival = get_object_or_404(_base_qs(), pk=pk)
    if not _can_view(request.user, arrival):
        return Response({"error": "Shipment not found."}, status=status.HTTP_404_NOT_FOUND)
    staff = arrival.booking.user_id != request.user.id
    return Response(arrival_dict(arrival, staff=staff))


@transaction.non_atomic_requests  # the provider call must not run inside a request transaction
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def shipment_refresh(request, pk: int):
    arrival = get_object_or_404(_base_qs(), pk=pk)
    owner = arrival.booking.user_id == request.user.id
    if not owner and not (_is_staff(request.user) and _can_view(request.user, arrival)):
        return Response({"error": "Shipment not found."}, status=status.HTTP_404_NOT_FOUND)
    trigger = LookupTrigger.USER if owner else LookupTrigger.STAFF
    result = services.lookup(arrival.pk, trigger, actor=request.user)
    arrival = _base_qs().get(pk=arrival.pk)
    body = {"arrival": arrival_dict(arrival, staff=not owner), "checked": result.performed}
    if not result.performed:
        body["message"] = refusal_message(result.refused)
        code = status.HTTP_429_TOO_MANY_REQUESTS if result.refused in ("too_soon", "daily_limit", "in_progress") else status.HTTP_200_OK
        return Response(body, status=code)
    body["message"] = "Shipment checked." if result.outcome == "SUCCESS" else (
        "The courier could not be checked right now. The last known status is shown."
    )
    return Response(body)


# -- Main Administrator ----------------------------------------------------------------------------------------------

def _require_main_admin(request):
    if not module_access.is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can manage courier tracking."},
                        status=status.HTTP_403_FORBIDDEN)
    return None


def _settings_dict(cfg: CourierTrackingSettings) -> dict:
    out = {name: getattr(cfg, name) for name in SETTINGS_FIELDS}
    out["catalog_refreshed_at"] = cfg.catalog_refreshed_at.isoformat() if cfg.catalog_refreshed_at else None
    out["updated_at"] = cfg.updated_at.isoformat() if cfg.updated_at else None
    return out


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_overview(request):
    denied = _require_main_admin(request)
    if denied:
        return denied
    from django.conf import settings as dj_settings

    now = timezone.now()
    active = SampleArrival.objects.filter(is_active=True, arrival_mode=ArrivalMode.COURIER)
    unresolved = active.exclude(status__in=TERMINAL_STATUSES).filter(
        Q(last_problem__gt="") | Q(status__in=[ShipmentStatus.ATTEMPT_FAILED, ShipmentStatus.EXCEPTION,
                                               ShipmentStatus.TRACKING_PROBLEM])
        | Q(expected_arrival_date__lt=timezone.localdate())
    )
    failures = TrackingLookup.objects.filter(reserved_at__gte=now - timedelta(days=30)).exclude(
        outcome__in=["SUCCESS", "RESERVED"]
    )
    cells = module_access.cells(MODULE)
    return Response(
        {
            "configured": bool(getattr(dj_settings, "TRACKCOURIER_API_KEY", "")),
            "webhooks_configured": webhooks.webhooks_configured(),
            "quota": quota.status().as_dict(),
            "settings": _settings_dict(CourierTrackingSettings.load()),
            "departments_enabled": sorted(d for d, c in cells.items() if c.enabled),
            "counts": {
                "active_courier": active.count(),
                "in_flight": active.exclude(status__in=TERMINAL_STATUSES).count(),
                "delivery_reported_unconfirmed": active.filter(status=ShipmentStatus.DELIVERY_REPORTED).count(),
                "manual": SampleArrival.objects.filter(is_active=True, arrival_mode=ArrivalMode.MANUAL).count(),
            },
            "unresolved": [arrival_dict(a, staff=True, include_history=False)
                           for a in _base_qs().filter(pk__in=unresolved.values("pk")).order_by("created_at")[:50]],
            "recent_failures": [
                {"at": l.reserved_at.isoformat(), "outcome": l.outcome, "code": l.provider_error_code,
                 "trigger": l.trigger, "arrival_id": l.arrival_id}
                for l in failures.order_by("-reserved_at")[:50]
            ],
            "alerts": [
                {"id": a.pk, "kind": a.kind, "message": a.message, "created_at": a.created_at.isoformat(),
                 "arrival_id": a.arrival_id}
                for a in CourierTrackingAlert.objects.filter(resolved_at__isnull=True)[:50]
            ],
            "webhook_events": {
                s: TrackingWebhookEvent.objects.filter(status=s, received_at__gte=now - timedelta(days=30)).count()
                for s in ("PROCESSED", "UNMATCHED", "IGNORED", "FAILED", "RECEIVED")
            },
            "audit": module_access_history(),
        }
    )


def module_access_history() -> list[dict]:
    from iic_booking.department_modules.services import history

    return history(module_key=MODULE, limit=50)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def admin_settings(request):
    denied = _require_main_admin(request)
    if denied:
        return denied
    serializer = SettingsSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"error": "Please check the highlighted fields.", "fields": serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)
    data = dict(serializer.validated_data)
    reason = data.pop("reason")
    with transaction.atomic():
        cfg = CourierTrackingSettings.objects.select_for_update().get(pk=CourierTrackingSettings.load().pk)
        before = {k: getattr(cfg, k) for k in data}
        changed = {k: v for k, v in data.items() if getattr(cfg, k) != v}
        if not changed:
            return Response({"settings": _settings_dict(cfg), "message": "Nothing changed."})
        for key, value in changed.items():
            setattr(cfg, key, value)
        cfg.updated_by = request.user
        cfg.save()
        module_audit.record(request.user, None, MODULE, "courier.settings",
                            old={k: before[k] for k in changed}, new=changed, reason=reason, request=request)
    return Response({"settings": _settings_dict(cfg), "message": "Settings saved."})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_resolve_alert(request, pk: int):
    denied = _require_main_admin(request)
    if denied:
        return denied
    updated = CourierTrackingAlert.objects.filter(pk=pk, resolved_at__isnull=True).update(
        resolved_at=timezone.now(), resolved_by=request.user
    )
    return Response({"resolved": bool(updated)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_clear_block(request):
    denied = _require_main_admin(request)
    if denied:
        return denied
    reason = str(request.data.get("reason") or "").strip()
    if len(reason) < 5:
        return Response({"error": "Give a reason (at least 5 characters)."}, status=status.HTTP_400_BAD_REQUEST)
    cleared = quota.clear_block(request.user)
    if cleared:
        module_audit.record(request.user, None, MODULE, "courier.clear_block", reason=reason, request=request)
    return Response({"cleared": cleared})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_refresh_catalog(request):
    denied = _require_main_admin(request)
    if denied:
        return denied
    ok = services.refresh_catalog()
    return Response({"refreshed": ok}, status=status.HTTP_200_OK if ok else status.HTTP_502_BAD_GATEWAY)


# -- webhook ---------------------------------------------------------------------------------------------------------

@transaction.non_atomic_requests  # the event row commits before 2xx is returned
@csrf_exempt
@api_view(["POST"])
@permission_classes([AllowAny])
def trackcourier_webhook(request):
    raw = request.body  # read before anything parses it
    try:
        event = webhooks.accept(
            raw,
            request.headers.get("X-Trackcourier-Signature", ""),
            mode_header=request.headers.get("X-Webhook-Mode", ""),
        )
    except webhooks.WebhookRejected as exc:
        return Response({"error": exc.message}, status=exc.status)
    transaction.on_commit(lambda: _queue_webhook(event.pk))
    return Response({"received": True})


def _queue_webhook(event_id: int) -> None:
    try:
        from .tasks import process_webhook_task

        process_webhook_task.delay(event_id)
    except Exception:  # noqa: BLE001 - the event is stored; the sweep processes it
        logger.warning("Courier webhook %s left for the sweep (queue unavailable)", event_id)
