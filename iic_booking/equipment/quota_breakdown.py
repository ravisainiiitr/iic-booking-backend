"""
"Bookings counted toward this limit": the weekly / monthly minutes a user (or their faculty group) has
used, booking by booking.

Rows come from the same ``QuotaDimension`` querysets and per-booking minutes
(``booking_effective_quota_minutes``) that booking enforcement sums, so the total shown always equals the
usage a booking attempt is checked against. Fetched only on demand (the user clicks "View bookings
counted"), read-only, no row locks, cached briefly per subject / period.

Who sees what:
- the user themselves: their own bookings in full; for a faculty (group) limit, other group members'
  bookings with the member's name but without links, inputs or charges;
- the faculty who owns the wallet group, the Main Administrator, the Department Administrator of the
  equipment's department and the OIC of the equipment: every group member's bookings in full;
- anyone else: 403.
For legacy equipment-level limits (shared by every user of a type on one instrument) other users'
bookings are shown as "Another user" to everyone except staff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Optional

from django.core.cache import cache
from django.db.models import OuterRef, Q, Subquery
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.display import get_user_display_name
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType

from .models import Booking, BookingStatus, DailySlot, Equipment, QuotaType
from .quota_utils import (
    QUOTA_COUNTING_STATUSES,
    QuotaDecision,
    QuotaDimension,
    QuotaService,
    booking_effective_quota_minutes,
    booking_quota_reference_datetime,
    quota_limit_is_effectively_unlimited,
)

BREAKDOWN_CACHE_SECONDS = 30
NOT_COUNTED_LIMIT = 25
ANOTHER_USER = "Another user"

_NOT_COUNTED_REASONS = {
    BookingStatus.CANCELLED: "Cancelled – the time went back to the limit",
    BookingStatus.REFUNDED: "Refunded – not counted",
    BookingStatus.WAITLISTED: "Waitlisted – counts only once booked",
    BookingStatus.HOLD: "Urgent request on hold – not counted",
    BookingStatus.ABSENT: "Operator unavailable – refunded, not counted",
    BookingStatus.UNDER_MAINTENANCE: "Under maintenance – refunded, not counted",
    BookingStatus.OTHER_DISRUPTION: "Analysis not possible – refunded, not counted",
}


class BreakdownError(Exception):
    def __init__(self, message: str, http_status: int = status.HTTP_400_BAD_REQUEST):
        super().__init__(message)
        self.message = message
        self.http_status = http_status


# ----------------------------------------------------------------------------------------------
# Periods and labels (IST)
# ----------------------------------------------------------------------------------------------


def normalize_quota_type(value) -> Optional[str]:
    v = str(value or "").strip().upper()
    if v in ("WEEK", "WEEKLY"):
        return QuotaType.WEEKLY
    if v in ("MONTH", "MONTHLY"):
        return QuotaType.MONTHLY
    return None


def period_label(quota_type: str, start: datetime, end: datetime) -> str:
    """"Week of Mon 5 Oct – Sun 11 Oct 2026" or "October 2026" (local time)."""
    start, end = timezone.localtime(start), timezone.localtime(end)
    if quota_type == QuotaType.MONTHLY:
        return start.strftime("%B %Y")
    return f"Week of {start:%a} {start.day} {start:%b} – {end:%a} {end.day} {end:%b %Y}"


def _short_period_label(quota_type: str, at: datetime) -> str:
    start, _ = QuotaService._get_quota_period(quota_type, at)
    if quota_type == QuotaType.MONTHLY:
        return start.strftime("%B %Y")
    return f"the week of {start.day} {start:%b %Y}"


def _day_reference(day: date) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(12, 0)), timezone.get_current_timezone())


# ----------------------------------------------------------------------------------------------
# Which limit
# ----------------------------------------------------------------------------------------------


def applicable_dimensions(subject: User, equipment, quota_type: str) -> list[QuotaDimension]:
    """Limits of this period that apply to ``subject`` on ``equipment`` (enforcement order, configured only)."""
    if equipment.equipment_group_id:
        dims = QuotaService.group_quota_dimensions(subject, equipment.equipment_group, (quota_type,))
    else:
        dims = QuotaService.legacy_quota_dimensions(subject, equipment, (quota_type,))
    return [d for d in dims if d.limit_minutes > 0 or d.scope == "pool"]


def resolve_dimension(subject: User, equipment, quota_type: str, scope: Optional[str]) -> QuotaDimension:
    dims = applicable_dimensions(subject, equipment, quota_type)
    wanted = (scope or "").strip().lower()
    if wanted in ("faculty",):
        wanted = "group"
    if wanted:
        if not equipment.equipment_group_id and wanted in ("individual", "external", "pool"):
            wanted = "pool"
        dims = [d for d in dims if d.scope == wanted]
    if not dims:
        raise BreakdownError("No such booking limit applies here.", status.HTTP_404_NOT_FOUND)
    return dims[0]


def scope_from_failure_reason(reason: str, equipment) -> Optional[str]:
    text = (reason or "").lower()
    if "faculty" in text:
        return "group"
    if not equipment.equipment_group_id:
        return "pool"
    if "individual" in text or "external" in text:
        return "individual"
    return None


# ----------------------------------------------------------------------------------------------
# Rows
# ----------------------------------------------------------------------------------------------


def _slot_span(booking) -> tuple[Optional[datetime], Optional[datetime]]:
    slots = [s for s in booking.daily_slots.all() if s.start_datetime and s.end_datetime]
    if not slots:
        return None, None
    return min(s.start_datetime for s in slots), max(s.end_datetime for s in slots)


def _display_id(booking) -> str:
    vbid = (booking.virtual_booking_id or "").strip()
    if vbid:
        return vbid
    code = booking.equipment.code if booking.equipment_id else ""
    return f"{code}-{booking.booking_id}" if code else str(booking.booking_id)


def _status_label(value: str) -> str:
    try:
        return str(BookingStatus(value).label)
    except ValueError:
        return str(value or "").replace("_", " ").title()


def _row(booking, *, minutes: int, counted: bool, note: Optional[str]) -> dict:
    first, last = _slot_span(booking)
    return {
        "booking_id": booking.booking_id,
        "display_booking_id": _display_id(booking),
        "equipment_id": booking.equipment_id,
        "equipment_name": booking.equipment.name if booking.equipment_id else "",
        "equipment_code": booking.equipment.code if booking.equipment_id else "",
        "slot_start": first.isoformat() if first else None,
        "slot_end": last.isoformat() if last else None,
        "minutes": int(minutes),
        "counted": counted,
        "status": booking.status,
        "status_label": _status_label(booking.status),
        "user_id": booking.user_id,
        "user_name": get_user_display_name(booking.user) if booking.user_id else "",
        "note": note,
    }


def _not_counted_note(booking, quota_type: str) -> str:
    if booking.source_booking_id:
        return "Repeat sample – not counted"
    if booking.status in _NOT_COUNTED_REASONS:
        return _NOT_COUNTED_REASONS[booking.status]
    if booking.status in QUOTA_COUNTING_STATUSES and booking.quota_period_anchor_at:
        return (
            "Moved by a disruption or staff reschedule – counted in "
            f"{_short_period_label(quota_type, booking.quota_period_anchor_at)}"
        )
    return f"{_status_label(booking.status)} – not counted"


def _first_slot_subquery():
    return Subquery(
        DailySlot.objects.filter(booking_id=OuterRef("pk")).order_by("start_datetime").values("start_datetime")[:1]
    )


def build_quota_breakdown(
    subject: User,
    equipment,
    dim: QuotaDimension,
    reference_dt: datetime,
    *,
    exclude_booking_id: Optional[int] = None,
) -> dict:
    """Unredacted breakdown of one limit for one period (cached briefly)."""
    start, end = QuotaService._get_quota_period(dim.quota_type, reference_dt)
    cache_key = (
        f"quota-breakdown:v1:{subject.pk}:{equipment.pk}:{dim.scope}:{dim.scope_label}:"
        f"{start.date().isoformat()}:{exclude_booking_id or 0}"
    )
    try:
        cached = cache.get(cache_key)
    except Exception:
        cached = None
    if cached is not None:
        return cached

    counted_bookings = list(
        dim.bookings_in_period(start, end, exclude_booking_id)
        .select_related("equipment", "user")
        .prefetch_related("daily_slots")
        .order_by("quota_reference_at", "booking_id")
    )
    counted = []
    for b in counted_bookings:
        first, _ = _slot_span(b)
        note = None
        if b.quota_period_anchor_at and first and not (start <= first <= end):
            note = (
                "Moved by a disruption or staff reschedule – still counted here, in its original "
                f"{'month' if dim.quota_type == QuotaType.MONTHLY else 'week'}"
            )
        counted.append(_row(b, minutes=booking_effective_quota_minutes(b), counted=True, note=note))
    used = sum(r["minutes"] for r in counted)

    owner = QuotaService.faculty_wallet_owner(subject)
    if dim.scope == "group":
        member_users = list(dim.users)
    elif owner is not None:
        member_users = QuotaService._wallet_users(subject)
    else:
        member_users = [subject]

    # Bookings of the same people in this period that do not count, with the reason (repeat samples,
    # cancellations, refunds, waitlist, holds, and bookings moved here but counted in their original period).
    if dim.scope == "pool":
        candidates = Booking.objects.filter(equipment_id=equipment.pk, user=subject)
    else:
        candidates = Booking.objects.filter(user__in=list(dim.users), equipment_id__in=list(dim.equipment_ids))
    not_counted_qs = (
        candidates.annotate(first_slot_at=_first_slot_subquery())
        .filter(
            Q(first_slot_at__gte=start, first_slot_at__lte=end)
            | Q(quota_period_anchor_at__gte=start, quota_period_anchor_at__lte=end)
        )
        .exclude(booking_id__in=[r["booking_id"] for r in counted])
        .select_related("equipment", "user")
        .prefetch_related("daily_slots")
        .order_by("first_slot_at", "booking_id")
    )
    if exclude_booking_id is not None:
        not_counted_qs = not_counted_qs.exclude(booking_id=exclude_booking_id)
    not_counted_bookings = list(not_counted_qs[: NOT_COUNTED_LIMIT + 1])
    not_counted = [
        _row(b, minutes=booking_effective_quota_minutes(b), counted=False, note=_not_counted_note(b, dim.quota_type))
        for b in not_counted_bookings[:NOT_COUNTED_LIMIT]
    ]

    group = getattr(equipment, "equipment_group", None)
    data = {
        "equipment": {"id": equipment.pk, "name": equipment.name, "code": equipment.code},
        "equipment_group_name": group.name if group else None,
        "scope": dim.scope,
        "scope_label": dim.scope_label,
        "period": dim.quota_type,
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "period_label": period_label(dim.quota_type, start, end),
        "limit_minutes": int(dim.limit_minutes),
        "used_minutes": int(used),
        "effectively_unlimited": quota_limit_is_effectively_unlimited(dim.quota_type, dim.limit_minutes),
        "subject": {"id": subject.pk, "name": get_user_display_name(subject)},
        "group_owner": {"id": owner.pk, "name": get_user_display_name(owner)} if owner is not None else None,
        "group_member_ids": [u.pk for u in member_users],
        "group_members_count": len(dim.users) if dim.scope == "group" else None,
        "excluded_booking_id": exclude_booking_id,
        "counted": counted,
        "not_counted": not_counted,
        "not_counted_truncated": len(not_counted_bookings) > NOT_COUNTED_LIMIT,
        "computed_at": timezone.now().isoformat(),
    }
    try:
        cache.set(cache_key, data, BREAKDOWN_CACHE_SECONDS)
    except Exception:
        pass
    return data


# ----------------------------------------------------------------------------------------------
# Permissions and redaction
# ----------------------------------------------------------------------------------------------


def staff_can_view_equipment(viewer: User, equipment) -> bool:
    """Main Administrator; OIC of the equipment; Department Administrator of its department."""
    user_type = str(getattr(viewer, "user_type", "") or "").lower()
    if user_type == UserType.ADMIN:
        return True
    if user_type not in (UserType.MANAGER, UserType.DEPT_ADMIN):
        return False
    from .api_views import _get_equipment_ids_for_log_access

    allowed = _get_equipment_ids_for_log_access(viewer)
    return allowed is None or equipment.pk in allowed


def breakdown_access(viewer: User, subject: User, equipment) -> Optional[str]:
    """"staff" / "owner" (faculty of the wallet group) see everything; "self" sees own rows in full."""
    if staff_can_view_equipment(viewer, equipment):
        return "staff"
    owner = QuotaService.faculty_wallet_owner(subject)
    if owner is not None and owner.pk == viewer.pk:
        return "owner"
    if subject.pk == viewer.pk:
        return "self"
    return None


def present_breakdown(raw: dict, viewer: User, access: str) -> dict:
    """Viewer-specific copy: links only where the viewer may open the booking; other people hidden as needed."""
    full_all = access in ("staff", "owner")
    member_ids = set(raw.get("group_member_ids") or [])

    def present(row: dict) -> dict:
        own = row["user_id"] == viewer.pk
        full = full_all or own
        named = full or row["user_id"] in member_ids
        out = dict(row)
        out["is_viewer"] = own
        out["can_open"] = full
        if not full:
            out["booking_id"] = None
        if not named:
            out["user_name"] = ANOTHER_USER
            out["user_id"] = None
            out["display_booking_id"] = None
        return out

    counted = [present(r) for r in raw["counted"]]
    not_counted = [present(r) for r in raw["not_counted"] if full_all or r["user_id"] == viewer.pk]

    members = []
    if raw["scope"] == "group":
        totals: dict[int, dict] = {}
        for r in raw["counted"]:
            m = totals.setdefault(r["user_id"], {"user_id": r["user_id"], "name": r["user_name"], "minutes": 0, "bookings": 0})
            m["minutes"] += r["minutes"]
            m["bookings"] += 1
        members = sorted(totals.values(), key=lambda m: (-m["minutes"], m["name"]))
        for m in members:
            m["is_viewer"] = m["user_id"] == viewer.pk

    out = {k: v for k, v in raw.items() if k not in ("counted", "not_counted", "group_member_ids")}
    out.update(
        counted=counted,
        not_counted=not_counted,
        members=members,
        viewer_access=access,
        full_details=full_all,
    )
    return out


def with_request(data: dict, requested_minutes: Optional[int]) -> dict:
    requested = max(0, int(requested_minutes or 0))
    limit, used = data["limit_minutes"], data["used_minutes"]
    data = dict(data)
    data["requested_minutes"] = requested
    data["remaining_minutes"] = max(0, limit - used)
    data["over_by_minutes"] = max(0, used + requested - limit)
    return data


# ----------------------------------------------------------------------------------------------
# Structured quota failure in booking / reschedule / edit error responses
# ----------------------------------------------------------------------------------------------


def quota_failure_fields(
    decision: QuotaDecision,
    *,
    equipment,
    subject: User,
    booking_date: Optional[datetime],
    booking_id: Optional[int] = None,
) -> dict:
    """``{"code": "QUOTA_EXCEEDED", "quota": {...}}`` to merge into a 400 response (quota only for minute limits)."""
    fields: dict = {"code": "QUOTA_EXCEEDED"}
    failure = decision.failure
    if failure is None:
        return fields
    quota = failure.payload()
    ref = timezone.localtime(booking_date) if booking_date else timezone.localtime()
    quota.update(
        equipment_id=equipment.pk,
        equipment_name=equipment.name,
        user_id=subject.pk,
        date=ref.date().isoformat(),
        booking_id=booking_id,
        period_label=(
            period_label(failure.quota_type, failure.period_start, failure.period_end)
            if failure.period_start and failure.period_end
            else None
        ),
        # Older clients render nested objects through their "message" key.
        message=decision.error,
    )
    fields["quota"] = quota
    return fields


class QuotaExceededError(ValueError):
    """Raised inside the booking transaction so the outer handler can return the structured payload."""

    def __init__(self, message: str, fields: dict):
        super().__init__(message)
        self.fields = fields


# ----------------------------------------------------------------------------------------------
# Endpoint
# ----------------------------------------------------------------------------------------------


def _int_param(raw, name: str) -> Optional[int]:
    if raw in (None, ""):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise BreakdownError(f"Invalid {name}.")


def _date_param(raw) -> Optional[date]:
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip(), "%Y-%m-%d").date()
    except ValueError:
        raise BreakdownError("Invalid date. Use YYYY-MM-DD.")


_LOGGED_USED_RE = re.compile(r"current usage (\d+) min", re.IGNORECASE)
_LOGGED_REQUESTED_RE = re.compile(r"requested (\d+) min", re.IGNORECASE)
_LOGGED_LIMIT_RE = re.compile(r"(?:configured limit|>)\s*(\d+) min", re.IGNORECASE)


def parse_logged_quota_figures(reason: str) -> dict:
    """Usage, request and limit as written in the failure message (older rows: "... > 270 min")."""

    def grab(rx):
        m = rx.search(reason or "")
        return int(m.group(1)) if m else None

    return {
        "used_minutes": grab(_LOGGED_USED_RE),
        "requested_minutes": grab(_LOGGED_REQUESTED_RE),
        "limit_minutes": grab(_LOGGED_LIMIT_RE),
    }


def _parse_dt(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


@dataclass
class AttemptContext:
    subject: User
    equipment: Equipment
    quota_type: str
    scope: Optional[str]
    reference_dt: Optional[datetime]
    requested_minutes: Optional[int]
    attempted_at: datetime
    logged: dict
    failure_reason: str


def attempt_log_context(viewer: User, log_id: int) -> AttemptContext:
    """
    Who / what / which period a failed attempt was checked against. The period is that of the first
    requested slot (as enforcement checks it), never the attempt time: booking opens on Wednesday
    evening for the next week, so the attempt's own week is usually the wrong one.
    """
    from .api_views import _get_equipment_ids_for_log_access, check_operator_permission
    from .attempt_log_display import requested_slots
    from .models import BookingAttemptLog, BookingAttemptOutcome

    if not check_operator_permission(viewer):
        raise BreakdownError("Only admin and Officer in charge can view quota details of booking attempts.", 403)
    log = BookingAttemptLog.objects.select_related("user", "equipment", "equipment__equipment_group").filter(pk=log_id).first()
    if log is None:
        raise BreakdownError("Log entry not found.", 404)
    allowed = _get_equipment_ids_for_log_access(viewer)
    if allowed is not None and log.equipment_id not in allowed:
        raise BreakdownError("You do not have permission to view quota details for this equipment.", 403)
    reason = (log.failure_reason or "").strip()
    if log.outcome != BookingAttemptOutcome.FAILED or "quota" not in reason.lower():
        raise BreakdownError("This attempt did not fail on a booking limit.")
    quota_type = normalize_quota_type(
        "WEEKLY" if "weekly" in reason.lower() else "MONTHLY" if "monthly" in reason.lower() else ""
    )
    if quota_type is None:
        raise BreakdownError("Could not tell from the failure whether the weekly or monthly limit was reached.")
    info = log.additional_info if isinstance(log.additional_info, dict) else {}
    subject = log.user
    booked_for_id = info.get("booked_for_user_id")
    if booked_for_id not in (None, "") and str(booked_for_id) != str(log.user_id):
        subject = User.objects.filter(pk=booked_for_id).first() or subject
    reference = None
    for slot in requested_slots(info):
        reference = _parse_dt(slot.get("start_datetime"))
        if reference is not None:
            break
    if reference is None:
        reference = _parse_dt(info.get("start_time"))
    if reference is None and info.get("visible_week_start"):
        try:
            reference = _day_reference(datetime.strptime(str(info["visible_week_start"])[:10], "%Y-%m-%d").date())
        except ValueError:
            reference = None
    logged = parse_logged_quota_figures(reason)
    return AttemptContext(
        subject=subject,
        equipment=log.equipment,
        quota_type=quota_type,
        scope=scope_from_failure_reason(reason, log.equipment),
        reference_dt=reference,
        requested_minutes=log.duration_minutes if log.duration_minutes is not None else logged["requested_minutes"],
        attempted_at=log.requested_at or timezone.now(),
        logged=logged,
        failure_reason=reason,
    )


def _next_period_reference(quota_type: str, at: datetime) -> datetime:
    _, end = QuotaService._get_quota_period(quota_type, at)
    return end + timedelta(hours=12)


def attempt_reference_and_source(ctx: AttemptContext, dim: QuotaDimension) -> tuple[datetime, str]:
    """
    The requested slot's period when the log has it. Older rows without slots: the attempt's period or
    the next one (bookings open for the next week), whichever matches the usage written in the failure.
    """
    if ctx.reference_dt is not None:
        return ctx.reference_dt, "requested_slot"
    logged_used = ctx.logged.get("used_minutes")
    candidates = [ctx.attempted_at, _next_period_reference(ctx.quota_type, ctx.attempted_at)]
    if logged_used is not None:
        for candidate in candidates:
            start, end = QuotaService._get_quota_period(dim.quota_type, candidate)
            used = QuotaService._sum_booking_quota_minutes(dim.bookings_in_period(start, end))
            if used == logged_used:
                return candidate, "matched_usage"
    return ctx.attempted_at, "attempt_time"


def attempt_notes(ctx: AttemptContext, data: dict, source: str) -> dict:
    logged = ctx.logged
    return {
        "attempted_at": ctx.attempted_at.isoformat(),
        "period_source": source,
        "logged_used_minutes": logged.get("used_minutes"),
        "logged_limit_minutes": logged.get("limit_minutes"),
        "logged_requested_minutes": logged.get("requested_minutes"),
        "limit_changed": logged.get("limit_minutes") is not None and logged["limit_minutes"] != data["limit_minutes"],
        "usage_changed": logged.get("used_minutes") is not None and logged["used_minutes"] != data["used_minutes"],
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def quota_breakdown_view(request):
    """
    Bookings counted toward one weekly / monthly limit.

    Query params:
      equipment, period (week|month), scope (individual|group), date (YYYY-MM-DD, any day in the period);
      user_id: another user (their faculty, or Admin / OIC / Department Administrator of the equipment);
      booking_id: that booking's owner and equipment, without the booking itself (Edit inputs / reschedule;
        ``date`` defaults to the booking's own quota period);
      requested: minutes of the refused request, shown as "requested";
      log_id: a failed Booking Attempt Log entry (staff), computed for the attempt's period from current data.
    """
    params = request.query_params
    try:
        exclude_booking_id = None
        requested = _int_param(params.get("requested"), "requested")
        log_id = _int_param(params.get("log_id"), "log_id")
        booking_id = _int_param(params.get("booking_id"), "booking_id")
        scope = params.get("scope")

        if log_id is not None:
            ctx = attempt_log_context(request.user, log_id)
            subject, equipment, quota_type = ctx.subject, ctx.equipment, ctx.quota_type
            scope = scope or ctx.scope
            requested = requested if requested is not None else ctx.requested_minutes
            dim = resolve_dimension(subject, equipment, quota_type, scope)
            reference_dt, source = attempt_reference_and_source(ctx, dim)
            raw = build_quota_breakdown(subject, equipment, dim, reference_dt)
            data = with_request(present_breakdown(raw, request.user, "staff"), requested)
            data["historical"] = True
            data["attempt"] = attempt_notes(ctx, data, source)
            return Response(data, status=status.HTTP_200_OK)
        else:
            quota_type = normalize_quota_type(params.get("period"))
            if quota_type is None:
                raise BreakdownError("period must be week or month.")
            day = _date_param(params.get("date"))
            if booking_id is not None:
                booking = (
                    Booking.objects.select_related("user", "equipment", "equipment__equipment_group")
                    .filter(pk=booking_id)
                    .first()
                )
                if booking is None:
                    raise BreakdownError("Booking not found.", 404)
                subject, equipment = booking.user, booking.equipment
                exclude_booking_id = booking.pk
                reference_dt = (
                    _day_reference(day) if day else booking_quota_reference_datetime(booking) or timezone.now()
                )
            else:
                equipment_id = _int_param(params.get("equipment"), "equipment")
                if equipment_id is None:
                    raise BreakdownError("equipment is required.")
                equipment = get_object_or_404(Equipment.objects.select_related("equipment_group"), pk=equipment_id)
                user_id = _int_param(params.get("user_id"), "user_id")
                subject = request.user if user_id in (None, request.user.pk) else User.objects.filter(pk=user_id).first()
                if subject is None:
                    raise BreakdownError("User not found.", 404)
                reference_dt = _day_reference(day or timezone.localdate())

        access = breakdown_access(request.user, subject, equipment)
        if access is None:
            raise BreakdownError("You can only see bookings counted toward your own (or your group's) limit.", 403)
        dim = resolve_dimension(subject, equipment, quota_type, scope)
        raw = build_quota_breakdown(subject, equipment, dim, reference_dt, exclude_booking_id=exclude_booking_id)
    except BreakdownError as exc:
        return Response({"error": exc.message}, status=exc.http_status)

    data = with_request(present_breakdown(raw, request.user, access), requested)
    data["historical"] = False
    return Response(data, status=status.HTTP_200_OK)
