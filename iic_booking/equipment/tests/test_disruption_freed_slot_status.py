"""Freed slots show the action that freed them, for every way a disrupted booking ends and for every viewer.

Analysis Not Possible used to release its slots as Operator Absent. Now its slots become Other Reasons labelled
"Analysis Not Possible"; Operator Unavailable still releases them as Operator Absent.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.equipment import maintenance_policy
from iic_booking.equipment.maintenance_policy import (
    ANALYSIS_NOT_POSSIBLE_SLOT_LABEL,
    auto_cancel_expired_maintenance_bookings,
    disruption_freed_slot_fields,
)
from iic_booking.equipment.management.commands.fix_analysis_not_possible_slots import wrongly_marked_slots
from iic_booking.equipment.models import (
    Booking,
    BookingDisruptionKind,
    BookingStatus,
    DailySlot,
    EquipmentManager,
    EquipmentOperator,
    SlotStatus,
)
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

ACTIONS = {
    # action -> (endpoint, body, booking disruption kind, freed status, freed label)
    "operator_unavailable": (
        "absent", {"notes": "Operator on leave"}, BookingDisruptionKind.OPERATOR_ABSENT, SlotStatus.OPERATOR_ABSENT, None,
    ),
    "analysis_not_possible": (
        "other-disruption",
        {"reason": "Sample not suitable for this probe"},
        BookingDisruptionKind.OTHER_DISRUPTION,
        SlotStatus.BLOCKED,
        ANALYSIS_NOT_POSSIBLE_SLOT_LABEL,
    ),
}


class _Wallet:
    balance = 0

    def credit(self, **kwargs):
        return None


@pytest.fixture
def lab(monkeypatch):
    from iic_booking.equipment import tasks, waitlist
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    monkeypatch.setattr(tasks.send_manual_disruption_operational_followup, "apply_async", lambda *a, **k: None)
    monkeypatch.setattr(waitlist, "notify_waitlist_slots_available", lambda *a, **k: 0)
    monkeypatch.setattr(
        WalletRepository, "get_booking_wallet_target", staticmethod(lambda *a, **k: (_Wallet(), None))
    )
    f = _EgsFactory()
    eq = f.equipment()
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    operator = UserFactory(user_type=UserType.OPERATOR, department=f.department)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    admin = UserFactory(user_type=UserType.ADMIN, department=f.department)
    return SimpleNamespace(f=f, eq=eq, student=f.student(), oic=oic, operator=operator, admin=admin)


def _flagged(lab, action, *, by=None, days=3):
    endpoint, body, kind, _, _ = ACTIONS[action]
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=days))
    res = lab.f.client_for(by or lab.operator).post(f"/api/bookings/{booking.pk}/{endpoint}/", body, format="json")
    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING
    assert booking.disruption_kind == kind
    return booking, list(booking.daily_slots.values_list("id", flat=True))


def _assert_freed(slot_ids, action):
    _, _, _, want_status, want_label = ACTIONS[action]
    rows = list(DailySlot.objects.filter(id__in=slot_ids).values_list("booking_id", "status", "blocked_label"))
    assert rows and all(r == (None, want_status, want_label) for r in rows), rows


def _user_refund(lab, booking):
    return lab.f.client_for(lab.student).post(f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json")


def _staff_refund(lab, booking):
    return lab.f.client_for(lab.oic).post(f"/api/bookings/{booking.pk}/refund/", {"notes": "Refunded"}, format="json")


def _deadline_passes(lab, booking):
    Booking.objects.filter(pk=booking.pk).update(maintenance_decision_deadline_at=timezone.now() - timedelta(minutes=1))
    assert auto_cancel_expired_maintenance_bookings() >= 1
    return SimpleNamespace(status_code=200, data=None)


def _user_reschedule(lab, booking):
    target = lab.f.slot(lab.eq, lab.f.future(days=6, hour=15))
    return lab.f.client_for(lab.student).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"start_time": target.start_datetime.isoformat(), "end_time": target.end_datetime.isoformat()},
        format="json",
    )


RESOLUTIONS = {
    "user_refund": _user_refund,
    "staff_refund": _staff_refund,
    "deadline_passes": _deadline_passes,
    "user_reschedule": _user_reschedule,
}


@pytest.mark.parametrize("action", list(ACTIONS))
@pytest.mark.parametrize("resolution", list(RESOLUTIONS))
def test_freed_slots_keep_the_action_that_freed_them(lab, action, resolution):
    booking, slot_ids = _flagged(lab, action)
    # The slots stay with the booking (shown as awaiting the user's choice) until it is resolved.
    assert set(DailySlot.objects.filter(id__in=slot_ids).values_list("booking_id", flat=True)) == {booking.pk}

    res = RESOLUTIONS[resolution](lab, booking)

    assert res.status_code == 200, res.data
    _assert_freed(slot_ids, action)


@pytest.mark.parametrize("flagged_by", ["operator", "oic", "admin"])
def test_analysis_not_possible_by_any_staff_role_never_frees_as_operator_absent(lab, flagged_by):
    booking, slot_ids = _flagged(lab, "analysis_not_possible", by=getattr(lab, flagged_by))
    assert booking.disruption_release_slot_status == SlotStatus.BLOCKED
    assert _user_refund(lab, booking).status_code == 200
    _assert_freed(slot_ids, "analysis_not_possible")


def _viewers(lab):
    f = lab.f
    dept_admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=f.department)
    return {
        "anonymous": None,
        "student": lab.student,
        "faculty": UserFactory(user_type=UserType.FACULTY, department=f.department),
        "operator": lab.operator,
        "oic": lab.oic,
        "dept_admin": dept_admin,
        "admin": lab.admin,
    }


def test_every_viewer_sees_analysis_not_possible_and_operator_absent_as_marked(lab):
    anp_booking, anp_slots = _flagged(lab, "analysis_not_possible", days=3)
    oa_booking, oa_slots = _flagged(lab, "operator_unavailable", days=4)
    assert _user_refund(lab, anp_booking).status_code == 200
    assert _user_refund(lab, oa_booking).status_code == 200
    start = (lab.f.future(days=2)).date().isoformat()
    end = (lab.f.future(days=5)).date().isoformat()

    from rest_framework.test import APIClient

    for name, viewer in _viewers(lab).items():
        client = APIClient() if viewer is None else lab.f.client_for(viewer)
        res = client.get(f"/api/equipments/{lab.eq.pk}/slots/", {"start_date": start, "end_date": end})
        assert res.status_code == 200, (name, res.status_code)
        body = res.data if isinstance(res.data, list) else (res.data.get("slots") or res.data.get("results") or [])
        by_id = {row["id"]: row for row in body}
        for sid in anp_slots:
            row = by_id.get(sid)
            assert row is not None, (name, "analysis not possible slot missing")
            assert row["status"] == SlotStatus.BLOCKED, (name, row["status"])
            assert row.get("blocked_label") == ANALYSIS_NOT_POSSIBLE_SLOT_LABEL, (name, row.get("blocked_label"))
        for sid in oa_slots:
            row = by_id.get(sid)
            assert row is not None, (name, "operator absent slot missing")
            assert row["status"] == SlotStatus.OPERATOR_ABSENT, (name, row["status"])


def test_bookings_flagged_before_the_fix_also_free_as_analysis_not_possible(lab):
    """Bookings awaiting a choice since before the fix still carry Operator Absent as their release status."""
    booking, slot_ids = _flagged(lab, "analysis_not_possible")
    Booking.objects.filter(pk=booking.pk).update(disruption_release_slot_status=SlotStatus.OPERATOR_ABSENT)
    booking.refresh_from_db()
    assert disruption_freed_slot_fields(booking, disrupted=True) == {
        "status": SlotStatus.BLOCKED,
        "blocked_label": ANALYSIS_NOT_POSSIBLE_SLOT_LABEL,
    }
    assert _staff_refund(lab, booking).status_code == 200
    _assert_freed(slot_ids, "analysis_not_possible")


def test_repair_command_finds_only_analysis_not_possible_slots_left_as_operator_absent(lab):
    from django.core.management import call_command

    anp_booking, anp_slots = _flagged(lab, "analysis_not_possible", days=3)
    oa_booking, oa_slots = _flagged(lab, "operator_unavailable", days=4)
    assert _user_refund(lab, anp_booking).status_code == 200
    assert _user_refund(lab, oa_booking).status_code == 200
    # Simulate the old behaviour on the Analysis Not Possible slots.
    DailySlot.objects.filter(id__in=anp_slots).update(status=SlotStatus.OPERATOR_ABSENT, blocked_label=None)

    assert set(wrongly_marked_slots().values_list("id", flat=True)) == set(anp_slots)
    call_command("fix_analysis_not_possible_slots")  # dry run
    assert set(DailySlot.objects.filter(id__in=anp_slots).values_list("status", flat=True)) == {SlotStatus.OPERATOR_ABSENT}
    call_command("fix_analysis_not_possible_slots", "--apply")
    _assert_freed(anp_slots, "analysis_not_possible")
    _assert_freed(oa_slots, "operator_unavailable")


def test_staff_refund_of_a_normal_booking_still_reopens_its_slots(lab):
    booking = lab.f.booking(lab.student, lab.eq, lab.f.future(days=3))
    slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    assert _staff_refund(lab, booking).status_code == 200
    assert set(DailySlot.objects.filter(id__in=slot_ids).values_list("status", "blocked_label")) == {
        (maintenance_policy.released_slot_status_after_booking_freed(lab.eq), None)
    }
