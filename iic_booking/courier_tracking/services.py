"""Sample arrival registration: hand delivery, or a courier name and tracking number typed in by the user.

Automatic courier tracking (provider lookups, webhooks, monthly quota, status emails) was removed after the
31 Oct 2026 committee meeting, which did not approve its recurring cost. The courier and number are stored as entered
and shown to the lab; the "Sample Sent" trace event is still written for courier shipments, and staff confirm receipt
on the portal as before.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .constants import (
    MAX_COURIER_REGISTRATIONS_PER_BOOKING,
    MAX_COURIER_REGISTRATIONS_PER_USER_PER_DAY,
    ArrivalMode,
    ShipmentStatus,
)
from .eligibility import arrival_eligibility
from .models import Courier, SampleArrival
from .parsing import normalize_tracking_number, tracking_number_problem

logger = logging.getLogger(__name__)


class ArrivalError(Exception):
    """A user-facing validation failure. ``field`` names the input it concerns."""

    def __init__(self, message: str, *, field: str = "", code: str = "invalid", status: int = 400):
        super().__init__(message)
        self.message = message
        self.field = field
        self.code = code
        self.status = status


# -- courier list ----------------------------------------------------------------------------------------------------

BUILT_IN_COURIERS = (
    ("bluedart", "Blue Dart"),
    ("delhivery", "Delhivery"),
    ("dtdc", "DTDC"),
    ("indiapost", "India Post"),
    ("indiapostinternational", "India Post International"),
    ("speedpost", "Speed Post"),
    ("shreemaruti", "Shree Maruti"),
    ("arccarrier", "ARC Carrier"),
    ("professional", "Professional Courier"),
)


def _seed_built_in_couriers() -> None:
    if Courier.objects.exists():
        return
    now = timezone.now()
    for slug, name in BUILT_IN_COURIERS:
        Courier.objects.get_or_create(slug=slug, defaults={"name": name, "is_active": True, "last_seen_at": now})


def catalog() -> list[dict]:
    """Couriers to choose from: the stored list (the built-in list is stored the first time). Empty only when even
    that could not be stored; callers must then show an error, not an empty selector."""
    _seed_built_in_couriers()
    return list(Courier.objects.filter(is_active=True).order_by("name").values("slug", "name"))


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
                raise ArrivalError(elig.courier_reason or "Courier details cannot be entered for this booking.", field="arrival_mode",
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
                current.save()
                return current
        if mode == ArrivalMode.COURIER:
            _check_registration_limits(user, booking)
        if current is not None:
            current.is_active = False
            current.superseded_at = timezone.now()
            current.superseded_reason = f"replaced by {mode.lower()} declaration"
            current.save(update_fields=["is_active", "superseded_at", "superseded_reason", "updated_at"])
        arrival = SampleArrival.objects.create(**fields)
        if arrival.is_courier:
            _sample_sent_trace(booking, user, arrival)
    logger.info("Booking %s arrival declared: %s (arrival %s)", booking.pk, mode, arrival.pk)
    return arrival


def _check_registration_limits(user, booking) -> None:
    """Limits on changing courier details, so a booking's history stays readable for the lab. (Concurrent
    submissions for one booking are already serialised by the booking lock.)"""
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


# -- mode switch -----------------------------------------------------------------------------------------------------

def switch_to_manual(user, booking, *, notes: str = "", expected_arrival_date=None) -> SampleArrival:
    """External user changes a courier declaration to manual delivery. The courier record is kept (inactive)."""
    return register_arrival(user, booking, ArrivalInput(arrival_mode=ArrivalMode.MANUAL, notes=notes,
                                                        expected_arrival_date=expected_arrival_date))
