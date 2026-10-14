"""Analysis Not Possible from Booking details: the assigned Lab Operator has the same rule as the OIC,
including after the lab has accepted the sample."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from django.core import mail

from iic_booking.equipment import booking_audit
from iic_booking.equipment.maintenance_policy import (
    analysis_not_possible_scope_error,
    can_flag_analysis_not_possible,
)
from iic_booking.equipment.models import (
    Booking,
    BookingDisruptionKind,
    BookingEvent,
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    SampleTraceStatus,
)
from iic_booking.equipment.serializers import BookingEventSerializer
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def lab(monkeypatch):
    from iic_booking.equipment import tasks, waitlist

    monkeypatch.setattr(tasks.send_manual_disruption_operational_followup, "apply_async", lambda *a, **k: None)
    monkeypatch.setattr(waitlist, "notify_waitlist_slots_available", lambda *a, **k: 0)
    f = _EgsFactory()
    eq = f.equipment()
    other_eq = f.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    operator = UserFactory(user_type=UserType.OPERATOR, department=f.department)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    stranger = UserFactory(user_type=UserType.OPERATOR, department=f.department)
    EquipmentOperator.objects.create(equipment=other_eq, operator=stranger)
    return SimpleNamespace(f=f, eq=eq, student=f.student(), oic=oic, operator=operator, stranger=stranger)


def _accepted_booking(lab, days=1):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=days))
    BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_SENT)
    BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_ACCEPTED)
    return booking


def _flag(lab, user, booking, reason="Plasma torch failure"):
    return lab.f.client_for(user).post(
        f"/api/bookings/{booking.pk}/other-disruption/", {"reason": reason}, format="json"
    )


def test_assigned_operator_may_flag_after_sample_accepted_like_the_oic(lab):
    booking = _accepted_booking(lab)
    assert Booking.objects.get(pk=booking.pk).status == BookingStatus.BOOKED
    assert can_flag_analysis_not_possible(lab.operator, booking)
    assert can_flag_analysis_not_possible(lab.oic, booking)
    assert not can_flag_analysis_not_possible(lab.stranger, booking)
    assert not can_flag_analysis_not_possible(lab.student, booking)


def test_operator_flag_matches_oic_outcome_and_records_the_operator(lab):
    by_operator = _accepted_booking(lab, days=1)
    by_oic = _accepted_booking(lab, days=2)

    res = _flag(lab, lab.operator, by_operator)
    assert res.status_code == 200, res.data
    operator_mail = [m for m in mail.outbox if "Analysis Not Possible" in m.subject]
    assert len(operator_mail) == 1 and operator_mail[0].to == [lab.student.email]

    res = _flag(lab, lab.oic, by_oic)
    assert res.status_code == 200, res.data
    assert len([m for m in mail.outbox if "Analysis Not Possible" in m.subject]) == 2

    fields = (
        "status",
        "disruption_kind",
        "disruption_reason",
        "disruption_release_slot_status",
        "maintenance_disruption_flag",
        "maintenance_reschedule_extra_week",
    )
    a, b = (Booking.objects.values(*fields).get(pk=x.pk) for x in (by_operator, by_oic))
    assert a == b
    assert a["status"] == BookingStatus.DISRUPTION_PENDING
    assert a["disruption_kind"] == BookingDisruptionKind.OTHER_DISRUPTION
    assert Booking.objects.get(pk=by_operator.pk).maintenance_decision_deadline_at is not None

    event = BookingEvent.objects.get(booking=by_operator, new_status=BookingStatus.DISRUPTION_PENDING)
    assert (event.previous_status, event.created_by) == (BookingStatus.BOOKED, lab.operator)
    audit = BookingEventSerializer(event).data["audit"]
    assert audit["actor_role"] == booking_audit.ROLE_LAB_OPERATOR
    assert audit["reason_code"] == booking_audit.REASON_ANALYSIS_NOT_POSSIBLE
    assert audit["reason"] == "Plasma torch failure"
    oic_event = BookingEvent.objects.get(booking=by_oic, new_status=BookingStatus.DISRUPTION_PENDING)
    assert BookingEventSerializer(oic_event).data["audit"]["actor_role"] == booking_audit.ROLE_OIC


def test_unassigned_operator_and_users_are_refused(lab):
    booking = _accepted_booking(lab)

    res = _flag(lab, lab.stranger, booking)
    assert res.status_code == 403
    assert res.data["error"] == "You are not assigned to this equipment."
    res = _flag(lab, lab.student, booking)
    assert res.status_code == 403
    assert "can flag a booking as Analysis Not Possible" in res.data["error"]
    assert Booking.objects.get(pk=booking.pk).status == BookingStatus.BOOKED
    assert not [m for m in mail.outbox if "Analysis Not Possible" in m.subject]


@pytest.mark.parametrize("status", [BookingStatus.COMPLETED, BookingStatus.REFUNDED, BookingStatus.CANCELLED])
def test_operator_has_the_same_status_limits_as_the_oic(lab, status):
    booking = _accepted_booking(lab)
    Booking.objects.filter(pk=booking.pk).update(status=status)
    booking.refresh_from_db()
    assert not can_flag_analysis_not_possible(lab.operator, booking)
    assert not can_flag_analysis_not_possible(lab.oic, booking)
    res = _flag(lab, lab.operator, booking)
    assert res.status_code == 400
    assert "Only BOOKED or PENDING" in res.data["error"]


def test_operator_cannot_flag_twice(lab):
    booking = _accepted_booking(lab)
    assert _flag(lab, lab.operator, booking).status_code == 200
    booking.refresh_from_db()
    assert not can_flag_analysis_not_possible(lab.operator, booking)
    assert _flag(lab, lab.operator, booking).status_code == 400


def test_scope_errors_per_role(lab):
    booking = _accepted_booking(lab)
    admin = UserFactory(user_type=UserType.ADMIN)
    other_oic = UserFactory(user_type=UserType.MANAGER)
    assert analysis_not_possible_scope_error(admin, booking) is None
    assert analysis_not_possible_scope_error(lab.oic, booking) is None
    assert analysis_not_possible_scope_error(lab.operator, booking) is None
    assert analysis_not_possible_scope_error(other_oic, booking) == "You do not manage this equipment."
    assert analysis_not_possible_scope_error(lab.stranger, booking) == "You are not assigned to this equipment."
