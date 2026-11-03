"""Main Administrator override of a disruption outcome (e.g. Booking Not Utilized marked by mistake)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import booking_events
from iic_booking.equipment.models import (
    BookingDisruptionKind,
    BookingEvent,
    BookingSampleTrace,
    BookingStatus,
    SampleTraceStatus,
    SlotStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _url(booking):
    return f"/api/bookings/{booking.pk}/status-override/"


def _not_utilized(f):
    eq = f.equipment(booking_not_utilize_window_hours=24)
    booking = f.booking(f.student(), eq, timezone.now() - timedelta(hours=50))
    booking.daily_slots.update(status=SlotStatus.BOOKING_NOT_UTILIZED)
    booking.status = BookingStatus.BOOKING_NOT_UTILIZED
    booking.save(update_fields=["status"])
    BookingSampleTrace.objects.create(
        booking=booking, status=SampleTraceStatus.NOT_UTILIZED, reason="Booking marked as Not Utilized by staff."
    )
    return booking


@pytest.fixture
def dispatched(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(booking_events, "_dispatch_booking_event_notification", calls.append)
    return calls


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def main_admin(egs_factory):
    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


@pytest.mark.django_db
def test_options_offer_completed_for_not_utilized(egs_factory, main_admin):
    booking = _not_utilized(egs_factory)
    res = _client(main_admin).get(_url(booking))
    assert res.status_code == 200
    body = res.json()
    assert body["allowed"] is True
    assert [t["value"] for t in body["targets"]] == ["COMPLETED", "BOOKED"]
    assert body["current_status_label"] == "Booking Not Utilized"


@pytest.mark.django_db
def test_not_utilized_to_completed_updates_booking_slots_trace_history_and_emails_user(
    egs_factory, main_admin, dispatched, django_capture_on_commit_callbacks
):
    booking = _not_utilized(egs_factory)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        res = _client(main_admin).post(
            _url(booking), {"status": "COMPLETED", "reason": "Analysis was done; marked Not Utilized by mistake."},
            format="json",
        )
    assert res.status_code == 200, res.content
    assert res.json()["status"] == "COMPLETED"

    booking.refresh_from_db()
    assert booking.status == BookingStatus.COMPLETED
    assert booking.completed_at is not None
    assert set(booking.daily_slots.values_list("status", flat=True)) == {SlotStatus.BOOKED}
    latest_trace = booking.sample_trace_events.order_by("-id").first()
    assert latest_trace.status == SampleTraceStatus.COMPLETED
    assert "marked Not Utilized by mistake" in latest_trace.reason

    event = BookingEvent.objects.filter(booking=booking).order_by("-event_id").first()
    assert event.previous_status == BookingStatus.BOOKING_NOT_UTILIZED
    assert event.new_status == BookingStatus.COMPLETED
    assert event.created_by_id == main_admin.pk
    assert event.metadata["status_override"] is True
    assert "Main Administrator" in event.comment and "by mistake" in event.comment

    assert dispatched == [event.event_id]
    booking_events.send_booking_event_notification(event)
    to_user = [m for m in mail.outbox if booking.user.email in m.to]
    assert to_user, [m.to for m in mail.outbox]
    assert "Booking Status Updated" in to_user[0].subject


@pytest.mark.django_db
def test_no_email_when_admin_unticks_notify(egs_factory, main_admin, dispatched, django_capture_on_commit_callbacks):
    booking = _not_utilized(egs_factory)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        res = _client(main_admin).post(
            _url(booking),
            {"status": "COMPLETED", "reason": "Corrected after the lab confirmed.", "notify_user": False},
            format="json",
        )
    assert res.status_code == 200
    assert dispatched == []
    assert not [m for m in mail.outbox if booking.user.email in m.to]


@pytest.mark.django_db
def test_not_utilized_to_booked_reopens_and_clears_not_utilized_trace(egs_factory, main_admin):
    booking = _not_utilized(egs_factory)
    res = _client(main_admin).post(
        _url(booking), {"status": "BOOKED", "reason": "Samples arrived late; lab will analyse."}, format="json"
    )
    assert res.status_code == 200
    booking.refresh_from_db()
    assert booking.status == BookingStatus.BOOKED
    assert set(booking.daily_slots.values_list("status", flat=True)) == {SlotStatus.BOOKED}
    assert not booking.sample_trace_events.filter(status=SampleTraceStatus.NOT_UTILIZED).exists()


@pytest.mark.django_db
def test_disruption_pending_to_completed_clears_disruption_fields(egs_factory, main_admin):
    eq = egs_factory.equipment()
    booking = egs_factory.booking(egs_factory.student(), eq, timezone.now() - timedelta(hours=5))
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.maintenance_disruption_flag = True
    booking.maintenance_decision_deadline_at = timezone.now() + timedelta(days=2)
    booking.save()
    booking.daily_slots.update(status=SlotStatus.OPERATOR_ABSENT)

    res = _client(main_admin).post(
        _url(booking), {"status": "COMPLETED", "reason": "Operator was present; analysis completed."}, format="json"
    )
    assert res.status_code == 200, res.content
    booking.refresh_from_db()
    assert booking.status == BookingStatus.COMPLETED
    assert booking.disruption_kind is None
    assert booking.maintenance_disruption_flag is False
    assert booking.maintenance_decision_deadline_at is None
    assert set(booking.daily_slots.values_list("status", flat=True)) == {SlotStatus.BOOKED}


@pytest.mark.django_db
@pytest.mark.parametrize("user_type", [UserType.OPERATOR, UserType.STUDENT])
def test_only_main_admin_may_override(egs_factory, user_type):
    booking = _not_utilized(egs_factory)
    other = UserFactory(user_type=user_type, department=egs_factory.department, admin_approved=True)
    c = _client(other)
    assert c.get(_url(booking)).status_code == 403
    res = c.post(_url(booking), {"status": "COMPLETED", "reason": "Trying to change it."}, format="json")
    assert res.status_code == 403
    booking.refresh_from_db()
    assert booking.status == BookingStatus.BOOKING_NOT_UTILIZED


@pytest.mark.django_db
def test_reason_is_required(egs_factory, main_admin):
    booking = _not_utilized(egs_factory)
    res = _client(main_admin).post(_url(booking), {"status": "COMPLETED", "reason": "oops"}, format="json")
    assert res.status_code == 400
    assert "reason" in res.json()["error"]


@pytest.mark.django_db
def test_target_must_fit_the_current_outcome(egs_factory, main_admin):
    booking = _not_utilized(egs_factory)
    res = _client(main_admin).post(
        _url(booking), {"status": "REFUNDED", "reason": "Refund it instead please."}, format="json"
    )
    assert res.status_code == 400
    assert "Completed" in res.json()["error"]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "outcome", [BookingStatus.ABSENT, BookingStatus.OTHER_DISRUPTION, BookingStatus.UNDER_MAINTENANCE]
)
def test_refunded_outcomes_are_explained_not_changed(egs_factory, main_admin, outcome):
    eq = egs_factory.equipment()
    booking = egs_factory.booking(egs_factory.student(), eq, timezone.now() - timedelta(hours=5))
    booking.status = outcome
    booking.save(update_fields=["status"])
    c = _client(main_admin)
    body = c.get(_url(booking)).json()
    assert body["allowed"] is False and body["targets"] == []
    assert "refunded" in body["refusal"]
    res = c.post(_url(booking), {"status": "COMPLETED", "reason": "Analysis was actually done."}, format="json")
    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.status == outcome


@pytest.mark.django_db
def test_completed_booking_is_not_a_disruption(egs_factory, main_admin):
    eq = egs_factory.equipment()
    booking = egs_factory.booking(egs_factory.student(), eq, timezone.now() - timedelta(hours=5))
    booking.status = BookingStatus.COMPLETED
    booking.save(update_fields=["status"])
    body = _client(main_admin).get(_url(booking)).json()
    assert body["allowed"] is False
    assert "disruption" in body["refusal"]
