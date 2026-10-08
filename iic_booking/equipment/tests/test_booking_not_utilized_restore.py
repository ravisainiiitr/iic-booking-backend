"""Restoring bookings wrongly marked Booking Not Utilized before the equipment window."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.equipment import booking_not_utilized_restore as restore
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    BookingSampleTrace,
    BookingStatus,
    SampleTraceStatus,
    SlotStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _marked(f, window, ended_hours_ago, marked_hours_after_end, *, by=None):
    eq = f.equipment(
        booking_not_utilize_window_hours=window, sample_submission_lead_hours=24, sample_collect_deadline_hours=72
    )
    end = timezone.now() - timedelta(hours=ended_hours_ago)
    booking = f.booking(f.student(), eq, end - timedelta(minutes=60))
    booking.daily_slots.update(status=SlotStatus.BOOKING_NOT_UTILIZED)
    booking.status = BookingStatus.BOOKING_NOT_UTILIZED
    booking.save(update_fields=["status"])
    BookingSampleTrace.objects.create(
        booking=booking,
        status=SampleTraceStatus.NOT_UTILIZED,
        reason=(
            "Automatically marked as Booking Not Utilized: latest booked slot end_datetime was over 24 hours ago"
            if by is None
            else "Booking marked as Not Utilized by staff. No refund issued."
        ),
        created_by=by,
    )
    event = BookingEvent.objects.create(
        booking=booking,
        event_type=BookingEventType.STATUS_CHANGED,
        previous_status=BookingStatus.BOOKED,
        new_status=BookingStatus.BOOKING_NOT_UTILIZED,
        comment=(
            "Automatically marked as Booking Not Utilized (scheduled check). No refund issued."
            if by is None
            else "Booking marked as Not Utilized. No refund issued."
        ),
        created_by=by,
    )
    BookingEvent.objects.filter(pk=event.pk).update(created_at=end + timedelta(hours=marked_hours_after_end))
    return booking


@pytest.mark.django_db
def test_only_bookings_marked_before_the_window_qualify(egs_factory):
    wrong = _marked(egs_factory, 240, ended_hours_ago=40, marked_hours_after_end=26)
    correct_24 = _marked(egs_factory, 24, ended_hours_ago=40, marked_hours_after_end=26)
    window_now_over = _marked(egs_factory, 240, ended_hours_ago=300, marked_hours_after_end=26)
    staff = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)
    manual = _marked(egs_factory, 240, ended_hours_ago=40, marked_hours_after_end=2, by=staff)
    switched_off = _marked(egs_factory, 0, ended_hours_ago=40, marked_hours_after_end=26)

    found = {c.booking.pk for c in restore.find_candidates()}

    assert found == {wrong.pk, switched_off.pk}
    assert correct_24.pk not in found and window_now_over.pk not in found and manual.pk not in found


@pytest.mark.django_db
def test_restore_sets_booked_and_is_idempotent(egs_factory):
    booking = _marked(egs_factory, 240, ended_hours_ago=40, marked_hours_after_end=26)
    (candidate,) = restore.find_candidates()

    event = restore.restore_booking(candidate)

    booking.refresh_from_db()
    assert booking.status == BookingStatus.BOOKED
    assert set(booking.daily_slots.values_list("status", flat=True)) == {SlotStatus.BOOKED}
    assert not BookingSampleTrace.objects.filter(booking=booking).exists()
    assert event.created_by_id is None
    assert event.new_status == BookingStatus.BOOKED
    assert "240-hour window" in event.comment
    assert restore.restore_event(booking).pk == event.pk
    assert restore.restore_booking(candidate) is None
    assert restore.find_candidates() == []


@pytest.mark.django_db
def test_restore_emails_user_and_supervisor_once(egs_factory, monkeypatch, settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    booking = _marked(egs_factory, 240, ended_hours_ago=40, marked_hours_after_end=26)
    supervisor = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    monkeypatch.setattr(type(booking.user), "get_accessible_wallet", lambda self: SimpleNamespace(user=supervisor))
    (candidate,) = restore.find_candidates()
    event = restore.restore_booking(candidate)
    mail.outbox.clear()

    assert restore.send_restore_emails(booking, event) == 2
    assert sorted(m.to[0] for m in mail.outbox) == sorted([booking.user.email, supervisor.email])
    assert all(booking.virtual_booking_id in m.subject for m in mail.outbox)
    assert "restored to Booked" in mail.outbox[0].body

    event.refresh_from_db()
    assert restore.send_restore_emails(booking, event) == 0
    assert len(mail.outbox) == 2
