"""Weekly waitlist window: cleared 5 minutes before the opening, nobody joins until it opens, and a confirmed booking
silently withdraws its user from that equipment's waitlist."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.equipment import api_views, waitlist
from iic_booking.equipment.models import Booking, BookingStatus, WaitlistEntry
from iic_booking.facility_groups.tests.conftest import make_booking, make_equipment, make_user

pytestmark = pytest.mark.django_db


def wednesday(hour: int, minute: int, second: int = 0) -> datetime:
    today = timezone.localdate()
    day = today - timedelta(days=today.weekday()) + timedelta(days=2)
    return timezone.make_aware(datetime.combine(day, time(hour, minute, second)), timezone.get_current_timezone())


@pytest.fixture
def eq():
    return make_equipment(waitlist_queue_depth=5, slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))


@pytest.mark.parametrize("at,closed", [
    ((20, 30), False), ((20, 54, 59), False), ((20, 55), True), ((20, 59, 59), True), ((21, 0), False), ((21, 5), False),
])
def test_closed_only_from_855_until_900(eq, at, closed):
    assert bool(waitlist.waitlist_closed_window(eq, wednesday(*at))) is closed


def test_no_opening_time_means_never_closed():
    other = make_equipment(waitlist_queue_depth=5)
    assert waitlist.waitlist_closed_window(other, wednesday(20, 56)) is None


def test_not_closed_on_other_days(eq):
    assert waitlist.waitlist_closed_window(eq, wednesday(20, 56) + timedelta(days=1)) is None


@pytest.mark.parametrize("at,left", [((20, 30), 1), ((20, 54), 1), ((20, 55), 0), ((20, 59), 0), ((21, 0), 1)])
def test_cleared_at_855_not_before(eq, at, left):
    WaitlistEntry.objects.create(equipment=eq, user=make_user(user_type="student"), status="ACTIVE")
    with patch.object(waitlist, "notify_waitlist_slots_available") as notify:
        waitlist.clear_waitlist_due_before_reference(now_dt=wednesday(*at))
    assert WaitlistEntry.objects.filter(equipment=eq).count() == left
    assert notify.called is (left == 0)  # slots given back are still offered first


def test_joining_is_refused_while_closed_and_works_at_the_opening(eq):
    user = make_user(user_type="student")
    with patch("django.utils.timezone.now", return_value=wednesday(20, 57)):
        assert waitlist.add_user_to_waitlist(eq, user) == (False, None)
    assert not WaitlistEntry.objects.filter(equipment=eq).exists()
    with patch("django.utils.timezone.now", return_value=wednesday(21, 0)):
        assert waitlist.add_user_to_waitlist(eq, user) == (True, 1)


def test_failed_booking_while_closed_explains_and_sends_nothing(eq):
    user = make_user(user_type="student")
    with patch("django.utils.timezone.now", return_value=wednesday(20, 57)), \
            patch.object(api_views, "_schedule_unsuccessful_booking_waitlist_email") as email:
        out = api_views._enrich_failed_booking_response(
            eq, user, "Booking unsuccessful. All slots are occupied.", waitlist_on_failure=True, slot_unavailable_failure=True)
    assert out["waitlist_closed"] is True
    assert "closed from 8:55 PM until 9:00 PM" in out["error"]
    assert "waitlist_position" not in out and "waitlist_full" not in out
    email.assert_not_called()
    assert not WaitlistEntry.objects.filter(equipment=eq).exists()


def test_confirmed_booking_withdraws_silently_and_only_on_that_equipment(eq):
    user = make_user(user_type="student")
    other_eq = make_equipment(waitlist_queue_depth=5)
    other_user = make_user(user_type="student")
    WaitlistEntry.objects.create(equipment=eq, user=user, status="ACTIVE")
    WaitlistEntry.objects.create(equipment=other_eq, user=user, status="ACTIVE")
    WaitlistEntry.objects.create(equipment=eq, user=other_user, status="ACTIVE")
    mail.outbox.clear()
    with patch("iic_booking.communication.service.CommunicationService.send_email") as send:
        make_booking(user, eq, status=BookingStatus.BOOKED)
    assert not WaitlistEntry.objects.filter(equipment=eq, user=user).exists()
    assert WaitlistEntry.objects.filter(equipment=other_eq, user=user).exists()
    assert WaitlistEntry.objects.filter(equipment=eq, user=other_user).exists()
    send.assert_not_called()
    assert mail.outbox == []
    # The next person moves up.
    assert waitlist.active_waitlist_position(WaitlistEntry.objects.get(equipment=eq, user=other_user)) == 1


@pytest.mark.parametrize("before", [BookingStatus.HOLD, BookingStatus.PENDING_PAYMENT, BookingStatus.PENDING])
def test_withdrawn_when_a_pending_booking_becomes_confirmed(eq, before):
    user = make_user(user_type="student")
    b = make_booking(user, eq, status=before)
    WaitlistEntry.objects.create(equipment=eq, user=user, status="ACTIVE")
    b = Booking.objects.get(pk=b.pk)
    b.total_charge = Decimal("11.00")
    b.save()  # still not confirmed
    assert WaitlistEntry.objects.filter(equipment=eq, user=user).exists()
    b.status = BookingStatus.BOOKED
    b.save(update_fields=["status"])
    assert not WaitlistEntry.objects.filter(equipment=eq, user=user).exists()


def test_an_existing_confirmed_booking_does_not_withdraw_a_later_waitlist_request(eq):
    user = make_user(user_type="student")
    b = make_booking(user, eq, status=BookingStatus.BOOKED)
    WaitlistEntry.objects.create(equipment=eq, user=user, status="ACTIVE")
    b = Booking.objects.get(pk=b.pk)
    b.total_charge = Decimal("11.00")
    b.save()  # an edit
    b.status = BookingStatus.UNDER_MAINTENANCE
    b.save(update_fields=["status"])
    b.status = BookingStatus.BOOKED  # back after a disruption
    b.save(update_fields=["status"])
    assert WaitlistEntry.objects.filter(equipment=eq, user=user).exists()


def test_opt_out_record_is_kept(eq):
    user = make_user(user_type="student")
    WaitlistEntry.objects.create(equipment=eq, user=user, status="OPT_OUT")
    make_booking(user, eq, status=BookingStatus.BOOKED)
    assert WaitlistEntry.objects.filter(equipment=eq, user=user, status="OPT_OUT").exists()


def test_cannot_fulfil_entry_is_withdrawn(eq):
    user = make_user(user_type="student")
    WaitlistEntry.objects.create(equipment=eq, user=user, status="CANNOT_FULFILL")
    make_booking(user, eq, status=BookingStatus.BOOKED)
    assert not WaitlistEntry.objects.filter(equipment=eq, user=user).exists()


def test_a_cancelled_or_refused_booking_does_not_withdraw(eq):
    user = make_user(user_type="student")
    WaitlistEntry.objects.create(equipment=eq, user=user, status="ACTIVE")
    make_booking(user, eq, status=BookingStatus.CANCELLED)
    assert WaitlistEntry.objects.filter(equipment=eq, user=user).exists()


def test_schedule_migration_moves_the_task_to_every_minute_and_back():
    from importlib import import_module

    from django.apps import apps
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    mod = import_module("iic_booking.equipment.migrations.0261_waitlist_clear_every_minute")
    PeriodicTask.objects.filter(task="equipment.clear_waitlist_before_reference").delete()
    five, _ = IntervalSchedule.objects.get_or_create(every=5, period="minutes")
    PeriodicTask.objects.create(name=mod.TASK, task="equipment.clear_waitlist_before_reference", interval=five, enabled=True)

    mod.forwards(apps, None)
    task = PeriodicTask.objects.get(task="equipment.clear_waitlist_before_reference")
    assert (task.name, task.interval.every, task.enabled) == (mod.NEW_NAME, 1, True)

    mod.backwards(apps, None)
    task = PeriodicTask.objects.get(task="equipment.clear_waitlist_before_reference")
    assert (task.name, task.interval.every) == (mod.TASK, 5)
