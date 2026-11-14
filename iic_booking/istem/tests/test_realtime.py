"""Real-time I-STEM sync (12 Nov 2026): booking and registration events start the sync within seconds; the
5-minute sync stays as the safety net. Celery is mocked: no task runs and no network is used."""

from __future__ import annotations

from unittest import mock

import pytest
from django.core.cache import cache

from iic_booking.equipment.models import BookingStatus
from iic_booking.istem import realtime
from iic_booking.istem.models import IstemOutboxJob, IstemUserLink
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .support import apply_live, make_booking

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _clear_kick():
    cache.delete(realtime.KICK_KEY)
    yield
    cache.delete(realtime.KICK_KEY)


@pytest.fixture
def scheduled():
    with mock.patch("iic_booking.istem.tasks.run_istem_sync.apply_async") as apply_async:
        yield apply_async


def test_paid_fbr_booking_starts_the_sync_within_seconds(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    with django_capture_on_commit_callbacks(execute=True):
        make_booking(status=BookingStatus.BOOKED)
    scheduled.assert_called_once_with(countdown=realtime.KICK_DELAY_SECONDS)


def test_completion_starts_the_sync_for_fbr_execute(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    booking = make_booking(status=BookingStatus.BOOKED)
    cache.delete(realtime.KICK_KEY)
    scheduled.reset_mock()
    with django_capture_on_commit_callbacks(execute=True):
        booking.status = BookingStatus.COMPLETED
        booking.save(update_fields=["status"])
    scheduled.assert_called_once()


def test_events_in_quick_succession_share_one_run(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    with django_capture_on_commit_callbacks(execute=True):
        make_booking(status=BookingStatus.BOOKED)
        make_booking(status=BookingStatus.BOOKED)
    assert scheduled.call_count == 1


def test_nothing_is_scheduled_when_automatic_fbr_is_off_or_fbr_not_required(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=False)
    with django_capture_on_commit_callbacks(execute=True):
        make_booking(status=BookingStatus.BOOKED)
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    with django_capture_on_commit_callbacks(execute=True):
        booking = make_booking(status=BookingStatus.BOOKED)
        booking.charge_profile.require_istem_fbr = False
        booking.charge_profile.save(update_fields=["require_istem_fbr"])
        cache.delete(realtime.KICK_KEY)
        booking.save()
    assert scheduled.call_count == 1  # only the FBR-required booking above, before the flag was cleared


def test_external_user_gets_an_istem_user_at_registration(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True, ISTEM_USER_CREATION_ENABLED=True)
    with django_capture_on_commit_callbacks(execute=True):
        user = UserFactory(user_type=UserType.INSTITUTE, email_verified=True, admin_approved=True)
    user.refresh_from_db()
    assert user.is_active
    assert IstemUserLink.objects.filter(user=user).exists()
    assert IstemOutboxJob.objects.filter(dedupe_key=f"ENSURE_USER:{user.pk}").exists()
    scheduled.assert_called()


def test_internal_users_and_inactive_sign_ups_wait(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True, ISTEM_USER_CREATION_ENABLED=True)
    with django_capture_on_commit_callbacks(execute=True):
        student = UserFactory(user_type=UserType.STUDENT, email_verified=True, admin_approved=True)
        pending = UserFactory(user_type=UserType.INSTITUTE, email_verified=False, admin_approved=False)
    pending.refresh_from_db()
    assert not pending.is_active
    assert not IstemUserLink.objects.filter(user__in=[student, pending]).exists()
    scheduled.assert_not_called()


def test_no_user_is_created_while_user_creation_is_off(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True, ISTEM_USER_CREATION_ENABLED=False)
    with django_capture_on_commit_callbacks(execute=True):
        user = UserFactory(user_type=UserType.INSTITUTE, email_verified=True, admin_approved=True)
    assert not IstemUserLink.objects.filter(user=user).exists()


# --- 14 Nov 2026: cancellations and moves in real time; nothing sent during the peak booking window ------------------

def _on_istem(booking):
    from iic_booking.istem.choices import SyncState
    from iic_booking.istem.models import IstemBookingLink

    return IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CONFIRMED, request_id="95601",
                                           fbr_no="1976095601", confirmation_id="72010")


def test_cancelling_a_booking_on_istem_starts_the_sync(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    booking = make_booking(status=BookingStatus.BOOKED)
    _on_istem(booking)
    cache.delete(realtime.KICK_KEY)
    scheduled.reset_mock()
    with django_capture_on_commit_callbacks(execute=True):
        booking.status = BookingStatus.CANCELLED
        booking.save(update_fields=["status"])
    scheduled.assert_called_once_with(countdown=realtime.KICK_DELAY_SECONDS)


def test_a_cancelled_booking_never_sent_to_istem_starts_nothing(settings, scheduled, django_capture_on_commit_callbacks):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    booking = make_booking(status=BookingStatus.PENDING)
    with django_capture_on_commit_callbacks(execute=True):
        booking.status = BookingStatus.CANCELLED
        booking.save(update_fields=["status"])
    scheduled.assert_not_called()


def test_a_reschedule_event_of_a_booking_on_istem_starts_the_sync(settings, scheduled, django_capture_on_commit_callbacks):
    from iic_booking.equipment.models import BookingEvent, BookingEventType

    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    booking = make_booking(status=BookingStatus.BOOKED)
    _on_istem(booking)
    cache.delete(realtime.KICK_KEY)
    scheduled.reset_mock()
    with django_capture_on_commit_callbacks(execute=True):
        BookingEvent.objects.create(booking=booking, event_type=BookingEventType.RESCHEDULED, comment="moved")
    scheduled.assert_called_once()


def test_during_the_peak_window_one_run_is_scheduled_for_its_end(settings, scheduled, django_capture_on_commit_callbacks):
    from datetime import timedelta

    from django.utils import timezone

    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    ends_at = timezone.now() + timedelta(minutes=7)
    cache.delete(realtime.AFTER_PEAK_KEY)
    with mock.patch.object(realtime, "peak_ends_at", return_value=ends_at), django_capture_on_commit_callbacks(execute=True):
        make_booking(status=BookingStatus.BOOKED)
        make_booking(status=BookingStatus.BOOKED)
    scheduled.assert_called_once_with(eta=ends_at + timedelta(seconds=realtime.AFTER_PEAK_DELAY_SECONDS))
    cache.delete(realtime.AFTER_PEAK_KEY)


def test_the_scheduled_sync_sends_nothing_during_the_peak_window(settings, scheduled):
    from datetime import timedelta

    from django.utils import timezone

    from iic_booking.istem import tasks

    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    ends_at = timezone.now() + timedelta(minutes=3)
    cache.delete(realtime.AFTER_PEAK_KEY)
    with mock.patch.object(realtime, "peak_ends_at", return_value=ends_at), \
            mock.patch("iic_booking.istem.service.process_due_jobs") as process:
        assert tasks.run_istem_sync() == {"deferred_until": ends_at.isoformat()}
    process.assert_not_called()
    cache.delete(realtime.AFTER_PEAK_KEY)
