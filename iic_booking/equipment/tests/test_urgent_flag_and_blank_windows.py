"""Urgent bookings can be told apart and filtered in the booking lists, for every role. A blank (stored as 0) Booking
Not Utilize Window or Results deadline switches the matching automatic marking off."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment import tasks
from iic_booking.equipment.booking_not_utilized_service import apply_booking_not_utilized, auto_not_utilized_hours
from iic_booking.equipment.models import (
    Booking,
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    SampleTraceStatus,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
)
from iic_booking.equipment.results_deadline import AutomationState, safeguard_due_at
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _urgent(f, user, eq, booking, status=UrgentBookingRequestStatus.APPROVED):
    return UrgentBookingRequest.objects.create(user=user, equipment=eq, hold_booking=booking, status=status)


@pytest.fixture
def lists(egs_factory):
    f = egs_factory
    eq = f.equipment()
    student = f.student()
    urgent = f.booking(student, eq, f.future(days=2, hour=9))
    normal = f.booking(student, eq, f.future(days=2, hour=11))
    pending = f.booking(student, eq, f.future(days=2, hour=13))
    rejected = f.booking(student, eq, f.future(days=2, hour=15))
    _urgent(f, student, eq, urgent)
    _urgent(f, student, eq, pending, UrgentBookingRequestStatus.PENDING)
    _urgent(f, student, eq, rejected, UrgentBookingRequestStatus.REJECTED)
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, admin_approved=True, department=f.department)
    return dict(f=f, eq=eq, student=student, oic=oic, admin=admin, urgent=urgent, normal=normal, pending=pending,
                rejected=rejected, other=f.student())


def _rows(lists, user, **params):
    res = lists["f"].client_for(user).get("/api/bookings/", {"list_view": "true", **params})
    assert res.status_code == 200, res.data
    return {r["real_booking_id"]: r for r in res.data["bookings"]}


@pytest.mark.parametrize("role", ["student", "oic", "admin"])
def test_only_approved_urgent_bookings_are_flagged_for_every_role(lists, role):
    rows = _rows(lists, lists[role])
    assert rows[lists["urgent"].pk]["is_urgent"] is True
    for name in ("normal", "pending", "rejected"):
        assert rows[lists[name].pk]["is_urgent"] is False


@pytest.mark.parametrize("role", ["student", "oic", "admin"])
def test_urgent_filter_for_every_role(lists, role):
    assert set(_rows(lists, lists[role], urgent="1")) == {lists["urgent"].pk}


def test_filter_stays_within_what_the_user_can_see(lists):
    assert _rows(lists, lists["other"], urgent="1") == {}


@pytest.mark.parametrize("status", [BookingStatus.COMPLETED, BookingStatus.PENDING, BookingStatus.REFUNDED])
def test_completed_or_later_urgent_bookings_stay_urgent(lists, status):
    Booking.objects.filter(pk=lists["urgent"].pk).update(status=status)
    assert _rows(lists, lists["student"], urgent="1")[lists["urgent"].pk]["is_urgent"] is True


# --- blank windows -------------------------------------------------------------------------------------------------


def _state(enabled):
    return AutomationState(enabled=enabled, since=timezone.now() - timedelta(days=365) if enabled else None)


def _past_booking(f, **equipment):
    eq = f.equipment(**equipment)
    return f.booking(f.student(), eq, f.future(days=-5, hour=9))


@pytest.mark.parametrize("automation", [True, False])
def test_no_results_deadline_turns_the_safeguard_off_in_both_modes(egs_factory, automation):
    booking = _past_booking(egs_factory, results_deadline_value=0, operator_absent_disruption_after_booking_end_hours=48)
    end = booking.daily_slots.first().end_datetime
    due, _ = safeguard_due_at(booking, end, legacy_hours=48, state=_state(automation))
    assert due is None


def test_legacy_timer_still_applies_with_a_results_deadline(egs_factory):
    booking = _past_booking(egs_factory, results_deadline_value=2)
    end = booking.daily_slots.first().end_datetime
    due, _ = safeguard_due_at(booking, end, legacy_hours=48, state=_state(False))
    assert due == end + timedelta(hours=48)


@pytest.mark.parametrize("value,marked", [(0, False), (2, True)])
def test_scheduled_job_marks_operator_absent_only_with_a_results_deadline(egs_factory, monkeypatch, value, marked):
    from iic_booking.equipment import maintenance_policy

    calls = []
    monkeypatch.setattr(
        maintenance_policy, "apply_operator_absent_disruption_for_booking", lambda b, **k: calls.append(b.pk)
    )
    booking = _past_booking(
        egs_factory, results_deadline_value=value, operator_absent_disruption_after_booking_end_hours=1
    )
    trace = BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_ACCEPTED)
    BookingSampleTrace.objects.filter(pk=trace.pk).update(created_at=timezone.now() - timedelta(days=4))
    tasks.auto_mark_operator_absent_disruption_after_booking_end()
    assert bool(calls) is marked


def test_blank_not_utilized_window_never_marks_automatically_or_by_staff(egs_factory):
    booking = _past_booking(egs_factory, booking_not_utilize_window_hours=0)
    assert auto_not_utilized_hours(booking.equipment, booking) is None
    assert apply_booking_not_utilized(booking, actor=None, automated=True) is False
    booking.refresh_from_db()
    assert booking.status != BookingStatus.BOOKING_NOT_UTILIZED
