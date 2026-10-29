"""Sample arrival registration, courier lookups and applying tracking results.

Registration never calls the provider in the request: it saves the arrival, writes the existing "Sample Sent" trace
event (courier shipments only), and schedules the first lookup, which the quota manager admits. Lookups happen only
for active courier arrivals of external bookings in departments where Courier Tracking is on, never for terminal
shipments, and never more often than the configured intervals. A lookup's answer is applied to every active arrival
with the same courier and number, so one parcel shared by two bookings costs one request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from . import notifications, quota
from .client import ApiResult, TrackCourierClient
from .constants import (
    DETAIL_PROBLEM_OUTCOMES,
    LOOKUP_BOOKING_STATUSES,
    MAX_COURIER_REGISTRATIONS_PER_BOOKING,
    MAX_COURIER_REGISTRATIONS_PER_USER_PER_DAY,
    STAFF_REFRESH_DAILY_LIMIT,
    PRIORITY_STATUSES,
    TERMINAL_STATUSES,
    ArrivalMode,
    LookupOutcome,
    LookupTrigger,
    ShipmentStatus,
)
from .eligibility import arrival_eligibility, booking_is_external, department_tracking_on
from .models import Courier, CourierTrackingSettings, SampleArrival, TrackingLookup
from .parsing import NormalizedTracking, normalize_tracking_number, parse_track_response, tracking_number_problem

logger = logging.getLogger(__name__)

CATALOG_MAX_AGE = timedelta(hours=24)
# A lookup in progress holds its arrival this long (compare-and-swap on ``last_lookup_attempt_at``).
CLAIM_WINDOW = timedelta(minutes=5)


class ArrivalError(Exception):
    """A user-facing validation failure. ``field`` names the input it concerns."""

    def __init__(self, message: str, *, field: str = "", code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code
        self.status = status


# -- courier catalog -------------------------------------------------------------------------------------------------

def refresh_catalog(client: TrackCourierClient | None = None) -> bool:
    """Replace the catalog from ``GET /v1/couriers`` (not counted against the quota). Couriers missing from the new
    list are marked inactive, not deleted. Returns False (catalog unchanged) when the list cannot be loaded."""
    from .alerts import raise_alert
    from .models import AlertKind

    client = client or TrackCourierClient()
    result = client.list_couriers()
    if not result.ok or not result.data:
        raise_alert(AlertKind.CATALOG_UNAVAILABLE, f"catalog:{timezone.now():%Y-%m-%d}",
                    f"The supported-courier list could not be loaded from TrackCourier.io ({result.outcome}).",
                    notify=False)
        return False
    now = timezone.now()
    seen = set()
    with transaction.atomic():
        for item in result.data:
            seen.add(item["slug"])
            Courier.objects.update_or_create(
                slug=item["slug"], defaults={"name": item["name"], "is_active": True, "last_seen_at": now}
            )
        Courier.objects.exclude(slug__in=seen).update(is_active=False)
        CourierTrackingSettings.objects.filter(pk=CourierTrackingSettings.load().pk).update(catalog_refreshed_at=now)
    return True


def catalog(*, refresh_if_stale: bool = True) -> tuple[list[dict], bool]:
    """(couriers, fresh). An empty list means it has never been loaded: callers must show an error, not an empty
    selector. ``fresh`` is False when the stored list is older than a day and could not be refreshed."""
    cfg = CourierTrackingSettings.load()
    stale = not cfg.catalog_refreshed_at or timezone.now() - cfg.catalog_refreshed_at > CATALOG_MAX_AGE
    fresh = not stale
    if stale and refresh_if_stale:
        fresh = refresh_catalog()
    couriers = list(Courier.objects.filter(is_active=True).order_by("name").values("slug", "name"))
    return couriers, fresh


# -- registration ----------------------------------------------------------------------------------------------------

@dataclass
class ArrivalInput:
    arrival_mode: str
    courier_slug: str = ""
    tracking_number: str = ""
    sample_identifiers: str = ""
    dispatch_date: object = None
    expected_arrival_date: object = None
    notes: str = ""
    email_updates: bool = False


def _sample_sent_trace(booking, user, arrival: SampleArrival) -> None:
    """Record the dispatch in the existing sample timeline (what staff already watch), once per booking: a changed
    courier or number is kept in the arrival history, not added as another "Sample Sent" event."""
    from iic_booking.equipment.models import BookingSampleTrace, SampleTraceStatus

    if BookingSampleTrace.objects.filter(booking=booking, status=SampleTraceStatus.SAMPLE_SENT).exists():
        return
    BookingSampleTrace.objects.create(
        booking=booking,
        status=SampleTraceStatus.SAMPLE_SENT,
        sample_identifiers=arrival.sample_identifiers,
        tracking_id=f"{arrival.courier_name}: {arrival.tracking_number}",
        created_by=user,
    )


def register_arrival(user, booking, data: ArrivalInput) -> SampleArrival:
    """Record how the booking's sample will arrive (replacing any earlier declaration, which is kept as history)."""
    from iic_booking.equipment.models import Booking

    mode = (data.arrival_mode or "").upper()
    if mode not in ArrivalMode.values:
        raise ArrivalError("Choose how the sample will arrive.", field="arrival_mode")
    with transaction.atomic():
        # Lock the booking: concurrent submissions for one booking serialise here, so only one stays active.
        booking = Booking.objects.select_for_update(of=("self",)).select_related("equipment", "user").get(pk=booking.pk)
        elig = arrival_eligibility(user, booking)
        if not elig.may_declare:
            raise ArrivalError(elig.reasons[0] if elig.reasons else "Not allowed.", code="forbidden", status=403)
        fields = {
            "booking": booking,
            "user": user,
            "equipment": booking.equipment,
            "department_id": booking.equipment.internal_department_id,
            "arrival_mode": mode,
            "sample_identifiers": (data.sample_identifiers or "").strip()[:2000],
            "dispatch_date": data.dispatch_date,
            "expected_arrival_date": data.expected_arrival_date,
            "notes": (data.notes or "").strip()[:2000],
        }
        if data.dispatch_date and data.expected_arrival_date and data.expected_arrival_date < data.dispatch_date:
            raise ArrivalError("The expected arrival date cannot be before the dispatch date.", field="expected_arrival_date")
        if mode == ArrivalMode.COURIER:
            if not elig.courier_allowed:
                raise ArrivalError(elig.courier_reason or "Courier tracking is not available.", field="arrival_mode",
                                   code="courier_not_allowed", status=403)
            slug = (data.courier_slug or "").strip().lower()
            courier = Courier.objects.filter(slug=slug, is_active=True).first()
            if courier is None:
                raise ArrivalError("Choose a courier from the list.", field="courier_slug")
            raw = (data.tracking_number or "").strip()[:64]
            number = normalize_tracking_number(raw)
            problem = tracking_number_problem(slug, number)
            if problem:
                raise ArrivalError(problem, field="tracking_number")
            fields.update(
                courier_slug=courier.slug,
                courier_name=courier.name,
                tracking_number_raw=raw,
                tracking_number=number,
                status=ShipmentStatus.REGISTERED,
                email_updates=bool(data.email_updates),
                next_lookup_at=timezone.now(),
                status_changed_at=timezone.now(),
            )
        current = SampleArrival.objects.select_for_update().filter(booking=booking, is_active=True).first()
        if current is not None:
            same = (
                current.arrival_mode == mode
                and current.courier_slug == fields.get("courier_slug", "")
                and current.tracking_number == fields.get("tracking_number", "")
            )
            if same:
                # A repeat of the same declaration (double submit): update the optional details only.
                for key in ("sample_identifiers", "dispatch_date", "expected_arrival_date", "notes"):
                    setattr(current, key, fields[key])
                if mode == ArrivalMode.COURIER:
                    current.email_updates = fields["email_updates"]
                current.save()
                return current
        if mode == ArrivalMode.COURIER:
            _check_registration_limits(user, booking)
        if current is not None:
            current.is_active = False
            current.superseded_at = timezone.now()
            current.superseded_reason = f"replaced by {mode.lower()} declaration"
            current.next_lookup_at = None
            current.save(update_fields=["is_active", "superseded_at", "superseded_reason", "next_lookup_at", "updated_at"])
        arrival = SampleArrival.objects.create(**fields)
        if arrival.is_courier:
            _sample_sent_trace(booking, user, arrival)
            notifications.on_registered(arrival)
            transaction.on_commit(lambda: _dispatch_lookup(arrival.pk, LookupTrigger.INITIAL))
    logger.info("Booking %s arrival declared: %s (arrival %s)", booking.pk, mode, arrival.pk)
    return arrival


def _check_registration_limits(user, booking) -> None:
    """Each new courier registration may cost a lookup, so one user cannot spend the shared monthly quota by
    re-registering. (Concurrent submissions for one booking are already serialised by the booking lock.)"""
    if SampleArrival.objects.filter(booking=booking, arrival_mode=ArrivalMode.COURIER).count() >= MAX_COURIER_REGISTRATIONS_PER_BOOKING:
        raise ArrivalError(
            "The courier details of this booking have been changed too many times. Please contact the lab.",
            field="tracking_number", code="too_many_changes", status=429,
        )
    since = timezone.now() - timedelta(days=1)
    if SampleArrival.objects.filter(user=user, arrival_mode=ArrivalMode.COURIER, created_at__gte=since).count() >= MAX_COURIER_REGISTRATIONS_PER_USER_PER_DAY:
        raise ArrivalError(
            "You have registered several courier shipments today. Please try again tomorrow.",
            field="tracking_number", code="daily_registration_limit", status=429,
        )


def _dispatch_lookup(arrival_id: int, trigger: str) -> None:
    try:
        from .tasks import lookup_task

        lookup_task.delay(arrival_id, trigger)
    except Exception:  # noqa: BLE001 - next_lookup_at is set, so the scheduled sweep picks it up
        logger.warning("Courier lookup queue unavailable for arrival %s; left for the scheduled check", arrival_id)


# -- lookups ---------------------------------------------------------------------------------------------------------

@dataclass
class LookupResult:
    performed: bool
    outcome: str = ""
    refused: str = ""  # reason no request was made
    arrival: SampleArrival | None = None


def lookup_refusal(arrival: SampleArrival, trigger: str, *, actor=None) -> str:
    """Why this arrival may not be looked up now ("" when it may). Checked again under the quota lock's caller."""
    if not arrival.is_active or arrival.arrival_mode != ArrivalMode.COURIER:
        return "not_courier"
    booking = arrival.booking
    if not booking_is_external(booking):
        return "not_external"
    if str(booking.status) not in LOOKUP_BOOKING_STATUSES:
        return "booking_not_confirmed"
    if not department_tracking_on(arrival.department_id):
        return "department_disabled"
    if arrival.status in TERMINAL_STATUSES:
        return "terminal"
    if arrival.last_problem in DETAIL_PROBLEM_OUTCOMES and trigger in (LookupTrigger.SCHEDULED, LookupTrigger.USER):
        return "details_need_attention"
    cfg = CourierTrackingSettings.load()
    if arrival.lookup_count >= cfg.max_lookups_per_shipment and trigger != LookupTrigger.STAFF:
        return "lookup_limit"
    now = timezone.now()
    last = arrival.last_lookup_attempt_at
    if trigger == LookupTrigger.USER:
        if last and now - last < timedelta(hours=cfg.user_refresh_interval_hours):
            return "too_soon"
        since = now - timedelta(days=1)
        used_today = TrackingLookup.objects.filter(
            requested_by=actor, trigger=LookupTrigger.USER, reserved_at__gte=since
        ).count()
        if used_today >= cfg.user_refresh_daily_limit:
            return "daily_limit"
    elif trigger == LookupTrigger.STAFF:
        if last and now - last < timedelta(hours=1):
            return "too_soon"
        used_today = TrackingLookup.objects.filter(
            requested_by=actor, trigger=LookupTrigger.STAFF, reserved_at__gte=now - timedelta(days=1)
        ).count()
        if used_today >= STAFF_REFRESH_DAILY_LIMIT:
            return "daily_limit"
    elif trigger == LookupTrigger.SCHEDULED:
        if arrival.next_lookup_at is None or arrival.next_lookup_at > now:
            return "not_due"
    elif trigger == LookupTrigger.INITIAL:
        if arrival.lookup_count:
            return "already_looked_up"
    return ""


def _shared_recent_result(arrival: SampleArrival) -> SampleArrival | None:
    """Another active arrival with the same courier and number, looked up successfully very recently."""
    cutoff = timezone.now() - timedelta(hours=1)
    return (
        SampleArrival.objects.filter(
            is_active=True,
            arrival_mode=ArrivalMode.COURIER,
            courier_slug=arrival.courier_slug,
            tracking_number=arrival.tracking_number,
            last_lookup_success_at__gte=cutoff,
        )
        .exclude(pk=arrival.pk)
        .order_by("-last_lookup_success_at")
        .first()
    )


def lookup(arrival_id: int, trigger: str, *, actor=None, client: TrackCourierClient | None = None) -> LookupResult:
    arrival = SampleArrival.objects.select_related("booking__user", "booking__equipment", "equipment", "department").filter(
        pk=arrival_id
    ).first()
    if arrival is None:
        return LookupResult(False, refused="missing")
    refused = lookup_refusal(arrival, trigger, actor=actor)
    if refused:
        return LookupResult(False, refused=refused, arrival=arrival)
    client = client or TrackCourierClient()
    if not client.configured:
        # No API key yet (e.g. before go-live): no reservation, no ledger row, no block; the admin page says why.
        return LookupResult(False, refused="not_configured", arrival=arrival)
    twin = _shared_recent_result(arrival)
    if twin is not None:
        # Duplicate suppression: reuse a fresh answer for the same parcel instead of spending a request.
        _apply(arrival, NormalizedTracking(status=twin.status, provider_state=twin.provider_state,
                                           status_label=twin.provider_status_label, checkpoints=list(twin.checkpoints),
                                           courier_has_no_record=twin.courier_has_no_record),
               source="shared", success_at=twin.last_lookup_success_at, event_at=twin.last_event_at)
        return LookupResult(False, refused="shared_recent_result", arrival=arrival)
    # Claim the arrival: of concurrent callers (user refresh, scheduled check, retry) only one gets past this.
    from django.db.models import Q

    now = timezone.now()
    previous_attempt = arrival.last_lookup_attempt_at
    claimed = SampleArrival.objects.filter(pk=arrival.pk).filter(
        Q(last_lookup_attempt_at__isnull=True) | Q(last_lookup_attempt_at__lt=now - CLAIM_WINDOW)
        | Q(last_lookup_attempt_at=previous_attempt)
    ).update(last_lookup_attempt_at=now)
    if not claimed:
        return LookupResult(False, refused="in_progress", arrival=arrival)
    # The safety reserve is for the first lookup of a booking's first shipment, not of each re-registration.
    allow_reserve = trigger != LookupTrigger.INITIAL or not SampleArrival.objects.filter(
        booking_id=arrival.booking_id, arrival_mode=ArrivalMode.COURIER, pk__lt=arrival.pk
    ).exists()
    reservation = quota.reserve(trigger=trigger, courier_slug=arrival.courier_slug,
                                tracking_number=arrival.tracking_number, arrival=arrival, requested_by=actor,
                                allow_reserve=allow_reserve)
    if reservation.lookup is None:
        SampleArrival.objects.filter(pk=arrival.pk, last_lookup_attempt_at=now).update(
            last_lookup_attempt_at=previous_attempt
        )
        _postpone(arrival, hours=12)
        return LookupResult(False, refused=reservation.refused, arrival=arrival)
    try:
        result = client.track(arrival.courier_slug, arrival.tracking_number)
    except Exception as exc:  # noqa: BLE001 - never leave a reservation open on an unexpected client error
        logger.exception("Courier lookup crashed for arrival %s", arrival.pk)
        result = ApiResult(False, LookupOutcome.SERVICE_ERROR, message=type(exc).__name__)
    twins = list(
        SampleArrival.objects.filter(
            is_active=True, arrival_mode=ArrivalMode.COURIER, courier_slug=arrival.courier_slug,
            tracking_number=arrival.tracking_number,
        ).exclude(pk=arrival.pk)
    )
    quota.complete(reservation.lookup, result, shared_with=len(twins) if result.ok else 0)
    _record_attempt(arrival, result)
    if result.ok:
        tracking = parse_track_response(result.data)
        event_at = _now_iso()
        for target in [arrival, *twins]:
            _apply(target, tracking, source="lookup", success_at=timezone.now(), event_at=event_at)
    return LookupResult(True, outcome=result.outcome, arrival=arrival)


def _postpone(arrival: SampleArrival, *, hours: int) -> None:
    if arrival.next_lookup_at is not None:
        SampleArrival.objects.filter(pk=arrival.pk).update(next_lookup_at=timezone.now() + timedelta(hours=hours))


def _record_attempt(arrival: SampleArrival, result: ApiResult) -> None:
    updates = {"last_lookup_attempt_at": timezone.now()}
    if result.outcome != LookupOutcome.SUCCESS:
        updates["last_problem"] = str(result.outcome)
        if result.outcome in DETAIL_PROBLEM_OUTCOMES:
            updates["next_lookup_at"] = None  # wait for the user to correct the details
            if arrival.status in (ShipmentStatus.REGISTERED, "",):
                updates["status"] = ShipmentStatus.TRACKING_PROBLEM
        else:
            # Transient or account problems: no blind retry; the next scheduled window decides.
            updates["next_lookup_at"] = timezone.now() + timedelta(hours=CourierTrackingSettings.load().priority_interval_hours)
    else:
        updates["last_problem"] = ""
    with transaction.atomic():
        locked = SampleArrival.objects.select_for_update().get(pk=arrival.pk)
        locked.lookup_count += 1
        for key, value in updates.items():
            setattr(locked, key, value)
        locked.save(update_fields=["lookup_count", *updates.keys(), "updated_at"])


def _next_lookup_at(status: str) -> object:
    if status in TERMINAL_STATUSES:
        return None
    cfg = CourierTrackingSettings.load()
    hours = cfg.priority_interval_hours if status in PRIORITY_STATUSES else cfg.scheduled_interval_hours
    return timezone.now() + timedelta(hours=hours)


def _now_iso() -> str:
    return timezone.now().astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply(
    arrival: SampleArrival, tracking: NormalizedTracking, *, source: str, success_at=None, event_at: str = ""
) -> tuple[SampleArrival, bool]:
    """Apply a normalised result; returns (arrival, applied). Replaces the checkpoint list when one is given (never
    appends), keeps it when the provider sent none, never moves a reported delivery back, and ignores an event older
    than one already applied (``event_at``: webhook enqueued_at or lookup time, ISO 8601 UTC). Queues messages for a
    meaningful change."""
    with transaction.atomic():
        locked = SampleArrival.objects.select_for_update(of=("self",)).select_related("booking__user", "equipment", "department").get(
            pk=arrival.pk
        )
        if event_at and locked.last_event_at and event_at < locked.last_event_at:
            if source == "webhook":
                locked.last_webhook_at = timezone.now()
                locked.save(update_fields=["last_webhook_at", "updated_at"])
            return locked, False
        if event_at:
            locked.last_event_at = event_at[:40]
        old_status = locked.status
        new_status = tracking.status or old_status
        if tracking.courier_has_no_record and not tracking.status:
            new_status = ShipmentStatus.EXCEPTION
        if old_status in TERMINAL_STATUSES and new_status not in TERMINAL_STATUSES:
            new_status = old_status  # a stale answer cannot reopen a finished shipment
        if tracking.checkpoints is not None:
            locked.checkpoints = tracking.checkpoints
            locked.latest_checkpoint = tracking.checkpoints[0] if tracking.checkpoints else {}
        locked.provider_state = tracking.provider_state or locked.provider_state
        locked.provider_status_label = tracking.status_label or locked.provider_status_label
        locked.courier_has_no_record = tracking.courier_has_no_record
        if new_status != old_status:
            locked.status = new_status
            locked.status_changed_at = timezone.now()
        if new_status == ShipmentStatus.DELIVERY_REPORTED and not locked.delivery_reported_at:
            locked.delivery_reported_at = timezone.now()
            latest = locked.latest_checkpoint or {}
            locked.delivery_event_at = (
                tracking.delivered_date or " ".join(x for x in (latest.get("date", ""), latest.get("time", "")) if x)
            )[:40]
        if success_at is not None and source != "webhook":
            locked.last_lookup_success_at = success_at
        if source == "webhook":
            locked.last_webhook_at = timezone.now()
        if locked.is_active:
            locked.next_lookup_at = _next_lookup_at(locked.status)
        locked.save()
        if locked.is_active and locked.status != old_status:
            notifications.on_status_change(locked, old_status)
    return locked, True


def apply_webhook(tracking_payload, *, courier_slug: str, tracking_number: str, enqueued_at: str = "") -> tuple[int, int]:
    """Apply a verified webhook to active courier arrivals with this courier and number. Returns (matched, applied).

    Shipments of departments where tracking was later switched off still get the update (it costs no quota) so a
    reported delivery reaches the lab staff; no lookups or reminders run for them."""
    arrivals = list(
        SampleArrival.objects.filter(
            is_active=True, arrival_mode=ArrivalMode.COURIER, courier_slug=courier_slug, tracking_number=tracking_number
        )
    )
    applied = 0
    for arrival in arrivals:
        if booking_is_external(arrival.booking):
            applied += int(_apply(arrival, tracking_payload, source="webhook", event_at=enqueued_at)[1])
    return len(arrivals), applied


# -- scheduled checks ------------------------------------------------------------------------------------------------

def due_arrivals(limit: int) -> list[int]:
    """Arrivals due a scheduled check, most useful first: never checked, then problem/out-for-delivery states, then
    the oldest successful check. Only active external courier shipments of enabled departments."""
    from django.db.models import Case, IntegerField, Value, When

    now = timezone.now()
    qs = (
        SampleArrival.objects.filter(
            is_active=True, arrival_mode=ArrivalMode.COURIER, next_lookup_at__isnull=False, next_lookup_at__lte=now,
            booking__status__in=LOOKUP_BOOKING_STATUSES,
        )
        .exclude(status__in=TERMINAL_STATUSES)
        .annotate(
            rank=Case(
                When(lookup_count=0, then=Value(0)),
                When(status__in=list(PRIORITY_STATUSES), then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("rank", "last_lookup_success_at", "created_at")
    )
    out: list[int] = []
    for arrival in qs.select_related("booking__user")[: limit * 3]:
        if not department_tracking_on(arrival.department_id) or not booking_is_external(arrival.booking):
            continue
        out.append(arrival.pk)
        if len(out) >= limit:
            break
    return out


def daily_budget() -> int:
    """Scheduled lookups allowed today: what is left above the reserve, spread over the rest of the month."""
    import calendar

    status = quota.status()
    if status.blocked_outcome:
        return 0
    spare = max(0, status.remaining - status.reserve)
    today = timezone.now().date()
    days_left = calendar.monthrange(today.year, today.month)[1] - today.day + 1
    return max(0, spare // max(1, days_left)) if spare >= days_left else (1 if spare else 0)


def run_scheduled_checks() -> dict:
    cfg = CourierTrackingSettings.load()
    if not cfg.scheduled_lookups_enabled:
        return {"skipped": "disabled"}
    quota.release_stale_reservations()
    budget = daily_budget()
    today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
    already = TrackingLookup.objects.filter(
        trigger__in=[LookupTrigger.SCHEDULED, LookupTrigger.INITIAL], reserved_at__gte=today_start
    ).count()
    allowed = max(0, budget - already)
    counts: dict[str, int] = {"budget": budget, "allowed": allowed}
    # First lookups of new registrations are always admitted (a booking's first may use the reserve); routine checks
    # use the budget.
    new_ids = list(
        SampleArrival.objects.filter(is_active=True, arrival_mode=ArrivalMode.COURIER, lookup_count=0,
                                     next_lookup_at__isnull=False, booking__status__in=LOOKUP_BOOKING_STATUSES)
        .exclude(status__in=TERMINAL_STATUSES)
        .values_list("pk", flat=True)[:50]
    )
    for pk in new_ids:
        r = lookup(pk, LookupTrigger.INITIAL)
        key = r.outcome if r.performed else f"refused:{r.refused}"
        counts[key] = counts.get(key, 0) + 1
    for pk in due_arrivals(allowed):
        r = lookup(pk, LookupTrigger.SCHEDULED)
        key = r.outcome if r.performed else f"refused:{r.refused}"
        counts[key] = counts.get(key, 0) + 1
    return counts


# -- mode switch -----------------------------------------------------------------------------------------------------

def switch_to_manual(user, booking, *, notes: str = "", expected_arrival_date=None) -> SampleArrival:
    """External user changes a courier declaration to manual delivery. The courier record is kept (inactive)."""
    return register_arrival(user, booking, ArrivalInput(arrival_mode=ArrivalMode.MANUAL, notes=notes,
                                                        expected_arrival_date=expected_arrival_date))
