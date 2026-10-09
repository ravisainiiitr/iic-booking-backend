"""Cancelled / refunded bookings keep their original start and end; booking list column sorting."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils.dateparse import parse_datetime

from iic_booking.communication.models import CommunicationLog
from iic_booking.communication.service import CommunicationService
from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
from iic_booking.equipment.booking_events import send_booking_event_notification
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    BookingSlotRange,
    BookingStatus,
    DailySlot,
    SlotStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _admin():
    return UserFactory(user_type=UserType.ADMIN, is_staff=True, admin_approved=True)


def _list(client, **params):
    res = client.get("/api/bookings/", params)
    assert res.status_code == 200, res.data
    return res.data["bookings"]


def _booking_row(rows, booking):
    return next(r for r in rows if r["real_booking_id"] == booking.pk)


def _dt(value):
    return parse_datetime(value) if isinstance(value, str) else value


@pytest.mark.django_db
def test_full_cancellation_keeps_original_start_and_end(egs_factory):
    eq = egs_factory.equipment()
    student = egs_factory.student()
    start = egs_factory.future(days=4, hour=9)
    booking = egs_factory.booking(student, eq, start, slot_count=2)
    slot_ids = list(booking.daily_slots.values_list("id", flat=True))

    perform_booking_cancellation(
        booking,
        slot_ids=slot_ids,
        should_refund=False,
        cancel_notes="",
        actor=_admin(),
        allow_started_slots=True,
    )

    booking.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED
    assert not booking.daily_slots.exists()
    stored = BookingSlotRange.objects.get(booking=booking)
    assert stored.start_datetime == start
    assert stored.end_datetime == start + timedelta(hours=2)

    client = egs_factory.client_for(_admin())
    for params in ({"list_view": "true"}, {"booking_id": booking.pk}):
        row = _booking_row(_list(client, **params), booking)
        assert _dt(row["start_time"]) == start
        assert _dt(row["end_time"]) == start + timedelta(hours=2)


@pytest.mark.django_db
def test_release_via_queryset_update_is_remembered_for_refund_and_reschedule_paths(egs_factory):
    eq = egs_factory.equipment()
    student = egs_factory.student()
    start = egs_factory.future(days=5, hour=11)
    booking = egs_factory.booking(student, eq, start, slot_count=3)

    DailySlot.objects.filter(booking=booking).update(booking_id=None, status=SlotStatus.AVAILABLE)
    type(booking).objects.filter(pk=booking.pk).update(status=BookingStatus.REFUNDED)

    stored = BookingSlotRange.objects.get(booking=booking)
    assert (stored.start_datetime, stored.end_datetime) == (start, start + timedelta(hours=3))


@pytest.mark.django_db
def test_partial_release_keeps_live_slot_times(egs_factory):
    eq = egs_factory.equipment()
    student = egs_factory.student()
    start = egs_factory.future(days=6, hour=9)
    booking = egs_factory.booking(student, eq, start, slot_count=2)
    first = booking.daily_slots.order_by("start_datetime").first()
    DailySlot.objects.filter(pk=first.pk).update(booking=None, status=SlotStatus.AVAILABLE)

    row = _booking_row(_list(egs_factory.client_for(_admin()), booking_id=booking.pk), booking)
    assert _dt(row["start_time"]) == start + timedelta(hours=1)
    assert _dt(row["end_time"]) == start + timedelta(hours=2)


@pytest.mark.django_db
def test_legacy_cancelled_booking_without_stored_range_returns_null_dates(egs_factory):
    eq = egs_factory.equipment()
    student = egs_factory.student()
    booking = egs_factory.booking(student, eq, egs_factory.future(days=2))
    DailySlot.objects.filter(booking=booking).update(booking=None)
    BookingSlotRange.objects.filter(booking=booking).delete()
    type(booking).objects.filter(pk=booking.pk).update(status=BookingStatus.CANCELLED)

    row = _booking_row(_list(egs_factory.client_for(_admin()), list_view="true"), booking)
    assert row["start_time"] is None
    assert row["end_time"] is None


@pytest.mark.django_db
def test_cancellation_email_uses_stored_dates(egs_factory):
    eq = egs_factory.equipment()
    student = egs_factory.student()
    start = egs_factory.future(days=3, hour=10)
    booking = egs_factory.booking(student, eq, start)
    booking.daily_slots.update(booking=None, status=SlotStatus.AVAILABLE)
    type(booking).objects.filter(pk=booking.pk).update(status=BookingStatus.CANCELLED)
    booking.refresh_from_db()
    event = BookingEvent.objects.create(
        booking=booking,
        event_type=BookingEventType.CANCELLED,
        previous_status=BookingStatus.BOOKED,
        new_status=BookingStatus.CANCELLED,
    )

    sent_log = SimpleNamespace(status=CommunicationLog.CommunicationStatus.SENT, error_message=None)
    with patch.object(CommunicationService, "send_email", return_value=sent_log) as send_email, patch.object(
        CommunicationService, "send_push_notification"
    ):
        send_booking_event_notification(event)

    contexts = [
        c.kwargs["template_context"]
        for c in send_email.call_args_list
        if c.kwargs.get("recipient") == student and "template_context" in c.kwargs
    ]
    assert contexts, "cancellation email was not sent to the booking user"
    assert contexts[0]["start_time"]
    assert contexts[0]["end_time"]


@pytest.fixture
def ordering_setup(egs_factory):
    eq_a = egs_factory.equipment(name="Alpha XRD")
    eq_b = egs_factory.equipment(name="beta TGA")
    alice = egs_factory.student()
    alice.name = "alice"
    alice.save(update_fields=["name"])
    bob = egs_factory.student()
    bob.name = "Bob"
    bob.save(update_fields=["name"])
    base = egs_factory.future(days=3, hour=9)
    early = egs_factory.booking(bob, eq_b, base, slot_count=1)
    late = egs_factory.booking(alice, eq_a, base + timedelta(days=2), slot_count=3)
    cancelled = egs_factory.booking(bob, eq_a, base + timedelta(days=1), slot_count=2)
    cancelled.daily_slots.update(booking=None, status=SlotStatus.AVAILABLE)
    type(cancelled).objects.filter(pk=cancelled.pk).update(status=BookingStatus.CANCELLED)
    return {
        "client": egs_factory.client_for(_admin()),
        "early": early,
        "late": late,
        "cancelled": cancelled,
        "mine": {early.pk, late.pk, cancelled.pk},
    }


def _ordered_ids(setup, ordering):
    rows = _list(setup["client"], list_view="true", ordering=ordering, limit=100)
    return [r["real_booking_id"] for r in rows if r["real_booking_id"] in setup["mine"]]


@pytest.mark.django_db
def test_order_by_start_time_includes_released_bookings(ordering_setup):
    s = ordering_setup
    asc = [s["early"].pk, s["cancelled"].pk, s["late"].pk]
    assert _ordered_ids(s, "start_time") == asc
    assert _ordered_ids(s, "-start_time") == list(reversed(asc))


@pytest.mark.django_db
def test_order_by_user_name_is_case_insensitive(ordering_setup):
    s = ordering_setup
    ids = _ordered_ids(s, "user_name")
    assert ids[0] == s["late"].pk
    assert set(ids[1:]) == {s["early"].pk, s["cancelled"].pk}
    assert _ordered_ids(s, "-user_name")[-1] == s["late"].pk


@pytest.mark.django_db
def test_order_by_equipment_duration_and_status(ordering_setup):
    s = ordering_setup
    assert _ordered_ids(s, "-equipment_name")[0] == s["early"].pk
    assert _ordered_ids(s, "duration") == [s["early"].pk, s["cancelled"].pk, s["late"].pk]
    assert _ordered_ids(s, "-duration")[0] == s["late"].pk
    assert set(_ordered_ids(s, "status")[:2]) == {s["early"].pk, s["late"].pk}
    assert _ordered_ids(s, "-status")[0] == s["cancelled"].pk


@pytest.mark.django_db
@pytest.mark.parametrize(
    "ordering",
    ["booking_ref", "-booking_ref", "user_email", "-user_phone", "supervisor_name", "-supervisor_name", "end_time",
     "total_charge", "-rating", "-created_at"],
)
def test_every_column_ordering_is_accepted(ordering_setup, ordering):
    assert set(_ordered_ids(ordering_setup, ordering)) == ordering_setup["mine"]


@pytest.mark.django_db
def test_order_by_equipment_code_ignores_name_and_case(egs_factory):
    client = egs_factory.client_for(_admin())
    student = egs_factory.student()
    base = egs_factory.future(days=3, hour=9)
    eq_b = egs_factory.equipment(name="Alpha", code="zzB XRD")
    eq_a = egs_factory.equipment(name="Zeta", code="ZZA TGA")
    eq_c = egs_factory.equipment(name="Beta", code="zzc nmr")
    b = egs_factory.booking(student, eq_b, base)
    a_old = egs_factory.booking(student, eq_a, base + timedelta(days=1))
    c = egs_factory.booking(student, eq_c, base + timedelta(days=2))
    a_new = egs_factory.booking(student, eq_a, base + timedelta(days=3))
    setup = {"client": client, "mine": {b.pk, a_old.pk, c.pk, a_new.pk}}

    assert _ordered_ids(setup, "equipment_code") == [a_new.pk, a_old.pk, b.pk, c.pk]
    assert _ordered_ids(setup, "-equipment_code") == [c.pk, b.pk, a_new.pk, a_old.pk]


@pytest.mark.django_db
def test_unknown_ordering_falls_back_to_newest_first(ordering_setup):
    s = ordering_setup
    assert _ordered_ids(s, "password; drop table") == _ordered_ids(s, "-created_at")
    assert _ordered_ids(s, "equipment__code") == _ordered_ids(s, "-created_at")
