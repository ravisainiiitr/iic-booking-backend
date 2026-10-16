"""Operator absent / lab disruption: the user may cancel (refund) or reschedule regardless of the threshold."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

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


@pytest.fixture
def quiet(monkeypatch):
    from iic_booking.equipment import tasks, waitlist

    monkeypatch.setattr(tasks.send_manual_disruption_operational_followup, "apply_async", lambda *a, **k: None)
    monkeypatch.setattr(waitlist, "notify_waitlist_slots_available", lambda *a, **k: 0)


def test_operator_unavailable_gives_the_user_five_days(lab, quiet, mailoutbox):
    from iic_booking.equipment.maintenance_policy import apply_operator_disruption_pending_from_staff

    f, eq, student = lab
    booking = f.booking(student, eq, f.future(days=1))
    before = timezone.now()

    apply_operator_disruption_pending_from_staff(booking)

    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING
    window = booking.maintenance_decision_deadline_at - before
    assert timedelta(days=5) <= window < timedelta(days=5, minutes=1)
    assert "marked Refunded" in mailoutbox[-1].body


def test_system_operator_absent_gives_the_user_five_days(lab, quiet):
    from iic_booking.equipment.maintenance_policy import apply_operator_absent_disruption_for_booking

    f, eq, student = lab
    booking = f.booking(student, eq, f.future(days=-3))
    marked_at = timezone.now()

    apply_operator_absent_disruption_for_booking(booking, triggered_at=marked_at)

    booking.refresh_from_db()
    assert booking.maintenance_decision_deadline_at == marked_at + timedelta(days=5)


def _expired_disruption(f, eq, student, *, deadline_delta):
    booking = f.booking(student, eq, f.future(days=-6), total_charge="100.00")
    _operator_absent(booking)
    booking.maintenance_decision_deadline_at = timezone.now() + deadline_delta
    booking.save(update_fields=["maintenance_decision_deadline_at"])
    return booking


def test_undecided_disruption_is_marked_refunded_with_full_refund(lab, quiet):
    from iic_booking.equipment.maintenance_policy import auto_cancel_expired_maintenance_bookings
    from iic_booking.equipment.models import BookingCancellation, CancellationReason
    from iic_booking.equipment.tests.fabrication_helpers import funded_student

    f, eq, _student = lab
    student, sub = funded_student(f, balance="1000.00")
    booking = _expired_disruption(f, eq, student, deadline_delta=-timedelta(minutes=1))

    assert auto_cancel_expired_maintenance_bookings() == 1

    booking.refresh_from_db()
    sub.refresh_from_db()
    assert booking.status == BookingStatus.REFUNDED
    assert not booking.maintenance_disruption_flag
    assert sub.balance == Decimal("1100.00")
    assert not booking.daily_slots.exists()
    row = BookingCancellation.objects.get(booking=booking)
    assert (row.reason, row.refund_amount) == (CancellationReason.DISRUPTION_DEADLINE, Decimal("100.00"))


def test_disruption_inside_the_window_is_left_alone(lab, quiet):
    from iic_booking.equipment.maintenance_policy import auto_cancel_expired_maintenance_bookings

    f, eq, student = lab
    booking = _expired_disruption(f, eq, student, deadline_delta=timedelta(days=2))

    assert auto_cancel_expired_maintenance_bookings() == 0
    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING


def test_booking_details_expose_disruption_fields(lab):
    f, eq, student = lab
    booking = f.booking(student, eq, f.future(days=1))
    _operator_absent(booking)

    data = BookingSerializer(booking).data

    assert data["maintenance_disruption_flag"] is True
    assert data["disruption_kind"] == BookingDisruptionKind.OPERATOR_ABSENT
    assert data["equipment_is_operational"] is True


# --- 5-day automatic refund: safety before scheduling -------------------------------------------------------------


def _charged(booking, sub, amount="100.00"):
    from iic_booking.communication.utils import booking_display_id_for_email

    ref = booking_display_id_for_email(booking)
    sub.debit(Decimal(amount), description=f"Booking #{booking.equipment.code} | Ref: {ref}")


def test_auto_refund_runs_once_and_credits_the_charged_sub_wallet(lab, quiet):
    from iic_booking.equipment.maintenance_policy import auto_cancel_expired_maintenance_bookings
    from iic_booking.equipment.models import BookingEvent, BookingEventType
    from iic_booking.equipment.tests.fabrication_helpers import funded_student
    from iic_booking.users.models.wallet import SubWalletTransaction

    f, eq, _student = lab
    student, sub = funded_student(f, balance="1000.00")
    booking = _expired_disruption(f, eq, student, deadline_delta=-timedelta(minutes=1))
    _charged(booking, sub)

    assert auto_cancel_expired_maintenance_bookings() == 1
    assert auto_cancel_expired_maintenance_bookings() == 0

    sub.refresh_from_db()
    assert sub.balance == Decimal("1000.00")
    credits = SubWalletTransaction.objects.filter(
        sub_wallet=sub, transaction_type=SubWalletTransaction.TransactionType.CREDIT, amount=Decimal("100.00")
    )
    assert credits.count() == 1
    event = BookingEvent.objects.get(booking=booking, event_type=BookingEventType.REFUNDED)
    assert event.metadata["refund_amount"] == "100.00"


def test_auto_refund_skips_bookings_the_user_already_decided(lab, quiet):
    from iic_booking.equipment.maintenance_policy import (
        auto_cancel_expired_maintenance_bookings,
        clear_disruption_policy_fields,
    )
    from iic_booking.equipment.tests.fabrication_helpers import funded_student

    f, eq, _student = lab
    student, sub = funded_student(f, balance="1000.00")
    refunded = _expired_disruption(f, eq, student, deadline_delta=-timedelta(hours=1))
    refunded.status = BookingStatus.REFUNDED
    refunded.save(update_fields=["status"])
    rescheduled = _expired_disruption(f, eq, student, deadline_delta=-timedelta(hours=1))
    clear_disruption_policy_fields(rescheduled, keep_quota_anchor=True)
    rescheduled.status = BookingStatus.BOOKED
    rescheduled.save()

    assert auto_cancel_expired_maintenance_bookings() == 0
    sub.refresh_from_db()
    assert sub.balance == Decimal("1000.00")
    rescheduled.refresh_from_db()
    assert rescheduled.status == BookingStatus.BOOKED


def test_auto_refund_dry_run_reports_counts_amounts_and_flags_without_changing_anything(lab, quiet, monkeypatch):
    from iic_booking.equipment import maintenance_policy
    from iic_booking.equipment.maintenance_policy import auto_cancel_dry_run
    from iic_booking.equipment.tests.fabrication_helpers import funded_student

    monkeypatch.setattr(maintenance_policy, "DECISION_WINDOW_POLICY_SINCE", "2000-01-01")
    f, eq, _student = lab
    student, sub = funded_student(f, balance="1000.00")
    ok = _expired_disruption(f, eq, student, deadline_delta=-timedelta(minutes=5))
    _charged(ok, sub)
    uncharged = _expired_disruption(f, eq, student, deadline_delta=-timedelta(minutes=5))
    _expired_disruption(f, eq, student, deadline_delta=timedelta(days=1))

    report = auto_cancel_dry_run()

    assert report["count"] == 2
    assert report["total_refund"] == "200.00"
    rows = {r["booking_id"]: r for r in report["rows"]}
    assert rows[ok.booking_id]["flags"] == []
    assert rows[ok.booking_id]["charged"] == "100.00"
    assert "no_charge_debit_found" in rows[uncharged.booking_id]["flags"]
    assert report["flagged"] == 1

    monkeypatch.setattr(maintenance_policy, "DECISION_WINDOW_POLICY_SINCE", "2999-01-01")
    assert "disrupted_before_5_day_policy" in auto_cancel_dry_run()["rows"][0]["flags"]
    ok.refresh_from_db()
    sub.refresh_from_db()
    assert ok.status == BookingStatus.DISRUPTION_PENDING and sub.balance == Decimal("900.00")
