"""Weekly / monthly quota across the booking lifecycle: input edits, cancellation, no-show, repeat
samples, disruption wait-and-reschedule, user vs staff reschedule and period boundaries."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
from iic_booking.equipment.booking_quota_summary import build_booking_quota_summary
from iic_booking.equipment.maintenance_policy import (
    apply_operator_absent_disruption_for_booking,
    apply_other_disruption_for_booking_manually,
)
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentGroupQuota,
    EquipmentManager,
    QuotaType,
)
from iic_booking.equipment.quota_utils import booking_quota_reference_datetime, get_quota_breakdown
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

WEEKLY_LIMIT = 120


def _monday(weeks_ahead: int):
    today = timezone.localdate()
    return today + timedelta(days=7 * weeks_ahead - today.weekday())


def _at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


def _weekly_used(user, equipment, day) -> int:
    summary = build_booking_quota_summary(user, equipment, day)
    return next(p for p in summary["periods"] if p["scope"] == "Individual Weekly")["used_minutes"]


def _body(slot):
    return {"start_time": slot.start_datetime.isoformat(), "end_time": slot.end_datetime.isoformat()}


@pytest.fixture
def quota_world(egs_factory, settings):
    settings.SKIP_BOOKING_QUOTA_CHECK = False
    group = egs_factory.group()
    eq = egs_factory.equipment(group, time_formula="A*30", enable_charge_recalculation=True)
    for quota_type, individual in ((QuotaType.WEEKLY, WEEKLY_LIMIT), (QuotaType.MONTHLY, 2000)):
        EquipmentGroupQuota.objects.create(
            equipment_group=group,
            quota_type=quota_type,
            internal_individual_quota_minutes=individual,
            internal_faculty_quota_minutes=5000,
            external_individual_quota_minutes=individual,
            external_faculty_quota_minutes=5000,
            is_enforced=True,
        )
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=True,
    )
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return SimpleNamespace(f=egs_factory, eq=eq, student=egs_factory.student(), oic=oic)


def _book(w, day, hour=10, *, minutes=60, slot_count=1, status=BookingStatus.BOOKED, **fields):
    booking = w.f.booking(w.student, w.eq, _at(day, hour), slot_count=slot_count, **fields)
    Booking.objects.filter(pk=booking.pk).update(total_time_minutes=minutes, status=status)
    booking.refresh_from_db()
    return booking


# --- which statuses count -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        BookingStatus.BOOKED,
        BookingStatus.COMPLETED,
        BookingStatus.BOOKING_NOT_UTILIZED,
        BookingStatus.PENDING_PAYMENT,
        BookingStatus.PENDING,
        BookingStatus.PROCESSING,
        BookingStatus.DISRUPTION_PENDING,
    ],
)
def test_slot_holding_and_no_show_bookings_count(quota_world, status):
    w = quota_world
    day = _monday(2)
    _book(w, day, status=status)
    assert _weekly_used(w.student, w.eq, day) == 60


@pytest.mark.parametrize(
    "status",
    [
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.ABSENT,
        BookingStatus.UNDER_MAINTENANCE,
        BookingStatus.OTHER_DISRUPTION,
        BookingStatus.WAITLISTED,
        BookingStatus.HOLD,
    ],
)
def test_freed_and_facility_side_outcomes_do_not_count(quota_world, status):
    w = quota_world
    day = _monday(2)
    _book(w, day, status=status)
    assert _weekly_used(w.student, w.eq, day) == 0


@pytest.mark.parametrize("should_refund", [False, True])
def test_cancellation_restores_quota_with_or_without_refund(quota_world, should_refund):
    w = quota_world
    day = _monday(2)
    booking = _book(w, day)
    assert _weekly_used(w.student, w.eq, day) == 60

    perform_booking_cancellation(
        booking,
        slot_ids=list(booking.daily_slots.values_list("id", flat=True)),
        should_refund=should_refund,
        cancel_notes="",
        actor=w.student,
        allow_started_slots=False,
    )

    booking.refresh_from_db()
    assert booking.status in (BookingStatus.CANCELLED, BookingStatus.REFUNDED)
    assert _weekly_used(w.student, w.eq, day) == 0


# --- period attribution ---------------------------------------------------------------------------


def test_booking_crossing_week_boundary_counts_once_in_its_first_week(quota_world):
    w = quota_world
    sunday = _monday(2) - timedelta(days=1)
    booking = _book(w, sunday, 23, minutes=120, slot_count=2)
    assert booking.daily_slots.count() == 2

    assert _weekly_used(w.student, w.eq, sunday) == 120
    assert _weekly_used(w.student, w.eq, _monday(2)) == 0


def test_quota_breakdown_lists_booking_on_its_reference_date(quota_world):
    w = quota_world
    day = _monday(2) + timedelta(days=2)
    booking = _book(w, day)

    data = get_quota_breakdown(w.student, w.eq, QuotaType.WEEKLY, _at(day, 12))

    assert data["total_minutes"] == 60
    assert [(e["real_booking_id"], e["date"]) for e in data["events"]] == [(booking.pk, day.isoformat())]


# --- repeat samples ---------------------------------------------------------------------------------


def test_repeat_sample_never_counts_and_its_reschedule_is_not_blocked(quota_world, egs_quiet_side_effects):
    w = quota_world
    week = _monday(2)
    original = _book(w, week - timedelta(days=7), status=BookingStatus.COMPLETED)
    repeat = _book(w, week, 9, source_booking=original)
    _book(w, week + timedelta(days=1), minutes=WEEKLY_LIMIT, slot_count=2)
    assert _weekly_used(w.student, w.eq, week) == WEEKLY_LIMIT

    new_slot = w.f.slot(w.eq, _at(week + timedelta(days=2), 15))
    res = w.f.client_for(w.student).post(f"/api/bookings/{repeat.pk}/user-reschedule/", _body(new_slot), format="json")

    assert res.status_code == 200, res.data
    assert _weekly_used(w.student, w.eq, week) == WEEKLY_LIMIT


# --- user and staff reschedule -----------------------------------------------------------------------


def test_user_reschedule_moves_usage_and_is_checked_in_the_new_week(quota_world, egs_quiet_side_effects):
    w = quota_world
    booking = _book(w, _monday(2))
    _book(w, _monday(3), 12, minutes=WEEKLY_LIMIT, slot_count=2)
    client = w.f.client_for(w.student)

    full_week_slot = w.f.slot(w.eq, _at(_monday(3) + timedelta(days=1), 15))
    blocked = client.post(f"/api/bookings/{booking.pk}/user-reschedule/", _body(full_week_slot), format="json")
    assert blocked.status_code == 400
    assert "Individual Weekly quota exceeded" in blocked.data["error"]

    free_week_slot = w.f.slot(w.eq, _at(_monday(4) + timedelta(days=1), 15))
    moved = client.post(f"/api/bookings/{booking.pk}/user-reschedule/", _body(free_week_slot), format="json")
    assert moved.status_code == 200, moved.data
    booking.refresh_from_db()
    assert booking.quota_period_anchor_at is None
    assert _weekly_used(w.student, w.eq, _monday(2)) == 0
    assert _weekly_used(w.student, w.eq, _monday(4)) == 60


@pytest.mark.parametrize("endpoint", ["reschedule", "user-reschedule"])
def test_staff_reschedule_keeps_the_original_week(quota_world, egs_quiet_side_effects, endpoint):
    w = quota_world
    booking = _book(w, _monday(2))
    _book(w, _monday(3), 12, minutes=WEEKLY_LIMIT, slot_count=2)
    new_slot = w.f.slot(w.eq, _at(_monday(3) + timedelta(days=1), 15))

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = w.f.client_for(w.oic).post(f"/api/bookings/{booking.pk}/{endpoint}/", _body(new_slot), format="json")

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.quota_period_anchor_at == _at(_monday(2), 10)
    assert _weekly_used(w.student, w.eq, _monday(2)) == 60
    assert _weekly_used(w.student, w.eq, _monday(3)) == WEEKLY_LIMIT


# --- disruption ----------------------------------------------------------------------------------------


def test_disruption_wait_and_reschedule_leaves_quota_unchanged(quota_world, egs_quiet_side_effects):
    w = quota_world
    booking = _book(w, _monday(2))
    _book(w, _monday(4), 12, minutes=WEEKLY_LIMIT, slot_count=2)

    apply_other_disruption_for_booking_manually(booking, reason="Detector fault")
    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING
    assert _weekly_used(w.student, w.eq, _monday(2)) == 60

    later_slot = w.f.slot(w.eq, _at(_monday(4) + timedelta(days=1), 15))
    res = w.f.client_for(w.student).post(f"/api/bookings/{booking.pk}/user-reschedule/", _body(later_slot), format="json")

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.status == BookingStatus.BOOKED
    assert booking.quota_period_anchor_at == _at(_monday(2), 10)
    assert _weekly_used(w.student, w.eq, _monday(2)) == 60
    assert _weekly_used(w.student, w.eq, _monday(4)) == WEEKLY_LIMIT

    apply_other_disruption_for_booking_manually(booking, reason="Again")
    booking.refresh_from_db()
    assert booking.quota_period_anchor_at == _at(_monday(2), 10)


def test_disruption_refund_frees_the_original_week(quota_world):
    w = quota_world
    booking = _book(w, _monday(2))
    apply_other_disruption_for_booking_manually(booking, reason="Detector fault")
    booking.refresh_from_db()

    perform_booking_cancellation(
        booking,
        slot_ids=list(booking.daily_slots.values_list("id", flat=True)),
        should_refund=True,
        cancel_notes="",
        actor=w.student,
        allow_started_slots=True,
    )

    booking.refresh_from_db()
    assert booking.quota_period_anchor_at is None
    assert _weekly_used(w.student, w.eq, _monday(2)) == 0


def test_operator_absent_disruption_anchors_the_original_period(quota_world):
    w = quota_world
    booking = _book(w, _monday(2))

    apply_operator_absent_disruption_for_booking(booking)

    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING
    assert booking.quota_period_anchor_at == _at(_monday(2), 10)
    assert booking_quota_reference_datetime(booking) == _at(_monday(2), 10)


# --- edit user inputs -------------------------------------------------------------------------------


def _edit(w, user, booking, values):
    return w.f.client_for(user).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": values}, format="json"
    )


def test_input_edit_increase_within_quota_consumes_more(quota_world):
    w = quota_world
    day = _monday(2)
    booking = _book(w, day, slot_count=2, input_values={"A": 2})

    res = _edit(w, w.student, booking, {"A": 4})

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.total_time_minutes == 120
    assert _weekly_used(w.student, w.eq, day) == 120


def test_input_edit_increase_over_quota_is_blocked(quota_world):
    w = quota_world
    day = _monday(2)
    booking = _book(w, day, slot_count=2, input_values={"A": 2})
    _book(w, day + timedelta(days=1), 14)

    res = _edit(w, w.student, booking, {"A": 3})

    assert res.status_code == 400
    assert res.data["code"] == "QUOTA_EXCEEDED"
    assert "30 more minute" in res.data["error"]
    assert "Individual Weekly quota exceeded" in res.data["error"]
    booking.refresh_from_db()
    assert booking.input_values["A"] == 2
    assert booking.total_time_minutes == 60
    assert _weekly_used(w.student, w.eq, day) == WEEKLY_LIMIT


def test_input_edit_decrease_releases_quota_even_when_over(quota_world):
    w = quota_world
    day = _monday(2)
    booking = _book(w, day, slot_count=2, input_values={"A": 2})
    _book(w, day + timedelta(days=1), 14, minutes=90, slot_count=2)

    res = _edit(w, w.student, booking, {"A": 1})

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.total_time_minutes == 30
    assert _weekly_used(w.student, w.eq, day) == 120


def test_staff_input_edit_overrides_quota(quota_world):
    w = quota_world
    day = _monday(2)
    booking = _book(w, day, slot_count=2, input_values={"A": 2})
    _book(w, day + timedelta(days=1), 14)

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = _edit(w, w.oic, booking, {"A": 4})

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.total_time_minutes == 120


def test_input_edit_minutes_sum_all_sample_sets(quota_world):
    from iic_booking.equipment.api_views import _calculate_input_values_minutes

    w = quota_world
    booking = _book(w, _monday(2), input_values={"A": 1})

    assert _calculate_input_values_minutes(booking, {"A": 1, "_sample_sets": [{"A": 2}, {"A": 1}]}) == 120


def test_input_edit_quota_ignores_repeat_samples(quota_world):
    from iic_booking.equipment.quota_utils import check_booking_minutes_change

    w = quota_world
    day = _monday(2)
    _book(w, day + timedelta(days=1), 14, minutes=WEEKLY_LIMIT, slot_count=2)
    original = _book(w, day - timedelta(days=7), status=BookingStatus.COMPLETED)
    repeat = _book(w, day, slot_count=2, source_booking=original)

    assert check_booking_minutes_change(repeat, 120) == (True, None)


def test_booking_quota_summary_uses_the_bookings_own_period(quota_world):
    w = quota_world
    original_week = _monday(2)
    booking = _book(w, original_week + timedelta(days=2), minutes=60)
    Booking.objects.filter(pk=booking.pk).update(quota_period_anchor_at=_at(original_week, 9))
    booking.daily_slots.update(
        start_datetime=_at(_monday(4), 10), end_datetime=_at(_monday(4), 11)
    )

    res = w.f.client_for(w.student).get(f"/api/equipments/{w.eq.pk}/my-booking-quota/?booking_id={booking.pk}")

    assert res.status_code == 200, res.data
    assert res.data["reference_date"] == original_week.isoformat()
    assert res.data["binding"]["used_minutes"] == 60
    assert res.data["booking"] == {"id": booking.pk, "counts_toward_quota": True, "minutes": 60}


def test_booking_quota_summary_is_private_to_the_booking_owner(quota_world):
    w = quota_world
    booking = _book(w, _monday(2))
    other = w.f.student()

    res = w.f.client_for(other).get(f"/api/equipments/{w.eq.pk}/my-booking-quota/?booking_id={booking.pk}")

    assert res.status_code == 403
