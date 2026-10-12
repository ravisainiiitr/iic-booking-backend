"""Operator absent / lab disruption: the user may cancel (refund) or reschedule regardless of the threshold."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from iic_booking.equipment.models import BookingDisruptionKind, BookingStatus
from iic_booking.equipment.serializers import BookingSerializer
from iic_booking.equipment.tests.conftest import _EgsFactory

pytestmark = pytest.mark.django_db

CANCEL_RESULT = {
    "released_slot_ids": [1],
    "refund_transaction": None,
    "refund_amount": Decimal("100.00"),
    "is_full_cancel": True,
}


def _operator_absent(booking):
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.maintenance_disruption_flag = True
    booking.save(update_fields=["status", "disruption_kind", "maintenance_disruption_flag"])


@pytest.fixture
def lab():
    f = _EgsFactory()
    eq = f.equipment(reschedule_hours_threshold=48)
    return f, eq, f.student()


@patch("iic_booking.equipment.api_views.perform_booking_cancellation", return_value=CANCEL_RESULT)
def test_operator_absent_booking_cancels_on_started_slots_inside_threshold(mock_cancel, lab):
    f, eq, student = lab
    booking = f.booking(student, eq, f.future(days=-1), total_charge="100.00")
    _operator_absent(booking)

    res = f.client_for(student).post(f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json")

    assert res.status_code == 200, res.data
    kwargs = mock_cancel.call_args.kwargs
    assert kwargs["allow_started_slots"] is True
    assert kwargs["should_refund"] is True


@patch("iic_booking.equipment.api_views.perform_booking_cancellation", return_value=CANCEL_RESULT)
def test_disruption_pending_without_flag_still_skips_threshold(mock_cancel, lab):
    f, eq, student = lab
    booking = f.booking(student, eq, f.future(days=1), total_charge="100.00")
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.save(update_fields=["status"])

    res = f.client_for(student).post(f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json")

    assert res.status_code == 200, res.data


@patch("iic_booking.equipment.api_views.perform_booking_cancellation", return_value=CANCEL_RESULT)
def test_repeat_booking_can_be_cancelled_only_during_a_disruption(mock_cancel, lab):
    f, eq, student = lab
    source = f.booking(student, eq, f.future(days=-10), total_charge="100.00")
    repeat = f.booking(student, eq, f.future(days=5), total_charge="0.00", source_booking=source)
    client = f.client_for(student)

    blocked = client.post(f"/api/bookings/{repeat.pk}/user-cancel/", {"refund": True}, format="json")
    assert blocked.status_code == 400
    assert "Repeat sample" in blocked.data["error"]

    _operator_absent(repeat)
    ok = client.post(f"/api/bookings/{repeat.pk}/user-cancel/", {"refund": True}, format="json")
    assert ok.status_code == 200, ok.data
    mock_cancel.assert_called_once()


def test_normal_booking_inside_threshold_is_still_blocked(lab):
    f, eq, student = lab
    booking = f.booking(student, eq, f.future(days=1), total_charge="100.00")

    res = f.client_for(student).post(f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json")

    assert res.status_code == 400
    assert "only available until" in res.data["error"]


def test_booking_details_expose_disruption_fields(lab):
    f, eq, student = lab
    booking = f.booking(student, eq, f.future(days=1))
    _operator_absent(booking)

    data = BookingSerializer(booking).data

    assert data["maintenance_disruption_flag"] is True
    assert data["disruption_kind"] == BookingDisruptionKind.OPERATOR_ABSENT
    assert data["equipment_is_operational"] is True
