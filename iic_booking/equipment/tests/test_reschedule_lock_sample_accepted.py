"""Once the lab accepts the sample, the booking user and their supervisor can no longer reschedule;
staff (OIC / Admin / Lab Operator with bookings.manage) still can."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from iic_booking.equipment.models import Booking, BookingSampleTrace, EquipmentManager, SampleTraceStatus
from iic_booking.equipment.reschedule_lock import (
    RESCHEDULE_LOCKED_SAMPLE_ACCEPTED,
    RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE,
    RESCHEDULE_OWNER_ONLY,
    RESCHEDULE_OWNER_ONLY_MESSAGE,
)
from iic_booking.equipment.serializers import BookingListSerializer, BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _setup(egs_factory):
    eq = egs_factory.equipment()
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(days=5, hour=10))
    new_slot = egs_factory.slot(eq, egs_factory.future(days=6, hour=11))
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=owner, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return SimpleNamespace(eq=eq, owner=owner, booking=booking, new_slot=new_slot, faculty=faculty, oic=oic)


def _trace(booking, *statuses):
    for st in statuses:
        BookingSampleTrace.objects.create(booking=booking, status=st)


def _body(slot):
    return {"start_time": slot.start_datetime.isoformat(), "end_time": slot.end_datetime.isoformat()}


def _user_reschedule(egs_factory, user, w):
    return egs_factory.client_for(user).post(
        f"/api/bookings/{w.booking.pk}/user-reschedule/", _body(w.new_slot), format="json"
    )


def _request_for(user):
    return SimpleNamespace(user=user)


def test_owner_can_reschedule_before_acceptance(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_SENT, SampleTraceStatus.FORWARDED_TO_LAB)

    res = _user_reschedule(egs_factory, w.owner, w)

    assert res.status_code == 200, res.data
    w.new_slot.refresh_from_db()
    assert w.new_slot.booking_id == w.booking.pk


@pytest.mark.parametrize(
    "statuses",
    [
        (SampleTraceStatus.SAMPLE_SENT, SampleTraceStatus.SAMPLE_ACCEPTED),
        (SampleTraceStatus.SAMPLE_ACCEPTED, SampleTraceStatus.PROCESSING),
        (SampleTraceStatus.COMPLETED,),
    ],
)
def test_owner_blocked_after_acceptance(egs_factory, egs_quiet_side_effects, statuses):
    w = _setup(egs_factory)
    _trace(w.booking, *statuses)
    old_slot_ids = set(w.booking.daily_slots.values_list("id", flat=True))

    res = _user_reschedule(egs_factory, w.owner, w)

    assert res.status_code == 400
    assert res.data["code"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED
    assert res.data["error"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE
    assert set(w.booking.daily_slots.values_list("id", flat=True)) == old_slot_ids
    w.new_slot.refresh_from_db()
    assert w.new_slot.booking_id is None


def test_supervisor_blocked_after_acceptance(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    res = _user_reschedule(egs_factory, w.faculty, w)

    assert res.status_code == 400
    assert res.data["code"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED


def test_unrelated_user_still_gets_permission_error(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    res = _user_reschedule(egs_factory, egs_factory.student(), w)

    assert res.status_code == 403
    assert "code" not in res.data


@pytest.mark.parametrize("endpoint", ["reschedule", "user-reschedule"])
def test_oic_can_still_reschedule_after_acceptance(egs_factory, egs_quiet_side_effects, endpoint):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = egs_factory.client_for(w.oic).post(
            f"/api/bookings/{w.booking.pk}/{endpoint}/", _body(w.new_slot), format="json"
        )

    assert res.status_code == 200, res.data
    w.new_slot.refresh_from_db()
    assert w.new_slot.booking_id == w.booking.pk


def test_admin_can_still_reschedule_after_acceptance(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True)

    res = egs_factory.client_for(admin).post(
        f"/api/bookings/{w.booking.pk}/reschedule/", _body(w.new_slot), format="json"
    )

    assert res.status_code == 200, res.data


# --- serializer ---------------------------------------------------------------------------------


def test_serializer_can_reschedule_before_acceptance(egs_factory):
    w = _setup(egs_factory)
    data = BookingSerializer(w.booking, context={"request": _request_for(w.owner)}).data
    assert data["can_reschedule"] is True
    assert data["reschedule_block_reason"] is None
    assert data["reschedule_block_message"] is None


def test_serializer_blocks_owner_and_supervisor_after_acceptance(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    for viewer in (w.owner, w.faculty):
        data = BookingSerializer(w.booking, context={"request": _request_for(viewer)}).data
        assert data["can_reschedule"] is False
        assert data["reschedule_block_reason"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED
        assert data["reschedule_block_message"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE


def test_serializer_supervisor_never_gets_reschedule(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    data = BookingSerializer(w.booking, context={"request": _request_for(w.faculty)}).data
    assert data["can_reschedule"] is False
    assert data["reschedule_block_reason"] == RESCHEDULE_OWNER_ONLY
    assert data["reschedule_block_message"] == RESCHEDULE_OWNER_ONLY_MESSAGE
    assert _user_reschedule(egs_factory, w.faculty, w).status_code == 403

    Booking.objects.filter(pk=w.booking.pk).update(maintenance_disruption_flag=True)
    w.booking.refresh_from_db()
    data = BookingSerializer(w.booking, context={"request": _request_for(w.faculty)}).data
    assert data["reschedule_block_reason"] == RESCHEDULE_OWNER_ONLY
    data = BookingSerializer(w.booking, context={"request": _request_for(w.owner)}).data
    assert data["can_reschedule"] is True


def test_list_serializer_supervisor_gets_owner_only(egs_factory):
    w = _setup(egs_factory)
    qs = Booking.objects.filter(pk=w.booking.pk)
    row = BookingListSerializer(qs, many=True, context={"request": _request_for(w.faculty)}).data[0]
    assert row["can_reschedule"] is False
    assert row["reschedule_block_reason"] == RESCHEDULE_OWNER_ONLY
    row = BookingListSerializer(qs, many=True, context={"request": _request_for(w.owner)}).data[0]
    assert row["can_reschedule"] is True
    assert row["reschedule_block_reason"] is None


def test_serializer_staff_viewing_others_keep_reschedule(egs_factory):
    w = _setup(egs_factory)
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        data = BookingSerializer(w.booking, context={"request": _request_for(w.oic)}).data
    assert data["can_reschedule"] is True
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True)
    data = BookingSerializer(w.booking, context={"request": _request_for(admin)}).data
    assert data["can_reschedule"] is True


def test_assistant_never_offers_supervisor_a_students_booking(egs_factory):
    from iic_booking.research_copilot.services.assistant import bookings as B
    from iic_booking.research_copilot.services.intelligence import booking_changes as changes
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    w = _setup(egs_factory)
    assert B.owned(w.faculty, w.booking.pk) is None
    assert changes.cancellable_bookings(w.faculty, for_reschedule=True) == []
    prep = booking_mut.prepare_reschedule(user=w.faculty, booking_id=w.booking.pk, slot_ids=[w.new_slot.pk])
    assert prep["ok"] is False
    assert prep["error"] == "BOOKING_FORBIDDEN"


def test_serializer_allows_staff_after_acceptance(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        data = BookingSerializer(w.booking, context={"request": _request_for(w.oic)}).data
    assert data["can_reschedule"] is True
    assert data["reschedule_block_reason"] is None


def test_serializer_without_request_leaves_can_reschedule_unknown(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    data = BookingSerializer(w.booking).data
    assert data["can_reschedule"] is None
    assert data["reschedule_block_reason"] is None


def test_list_serializer_marks_only_accepted_bookings(egs_factory):
    w = _setup(egs_factory)
    other = egs_factory.booking(w.owner, w.eq, egs_factory.future(days=8, hour=10))
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    _trace(other, SampleTraceStatus.SAMPLE_SENT)

    from iic_booking.equipment.models import Booking

    qs = Booking.objects.filter(pk__in=[w.booking.pk, other.pk]).order_by("pk")
    rows = {r["real_booking_id"]: r for r in BookingListSerializer(qs, many=True, context={"request": _request_for(w.owner)}).data}
    assert rows[w.booking.pk]["can_reschedule"] is False
    assert rows[w.booking.pk]["reschedule_block_reason"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED
    assert rows[other.pk]["can_reschedule"] is True


def test_my_bookings_list_api_exposes_lock(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    res = egs_factory.client_for(w.owner).get("/api/bookings/", {"list_view": "1"})
    assert res.status_code == 200, res.data
    row = next(r for r in res.data["bookings"] if r["real_booking_id"] == w.booking.pk)
    assert row["can_reschedule"] is False
    assert row["reschedule_block_reason"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED

    res = egs_factory.client_for(w.owner).get("/api/bookings/", {"booking_id": w.booking.pk})
    assert res.status_code == 200, res.data
    assert res.data["bookings"][0]["can_reschedule"] is False


# --- Booking Assistant ----------------------------------------------------------------------------


def test_assistant_does_not_offer_reschedule_after_acceptance(egs_factory):
    from iic_booking.research_copilot.services.assistant import bookings as B
    from iic_booking.research_copilot.services.assistant import daily
    from iic_booking.research_copilot.services.intelligence import booking_changes as changes

    w = _setup(egs_factory)
    before = B.eligibility(B.owned(w.owner, w.booking.pk))
    assert before["reschedule"] is True
    assert [r["booking_id"] for r in changes.cancellable_bookings(w.owner, for_reschedule=True)] == [w.booking.pk]

    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    b = B.owned(w.owner, w.booking.pk)
    elig = B.eligibility(b)
    assert elig["reschedule"] is False
    assert elig["reschedule_locked"] is True
    assert elig["cancel"] is False
    ops = [a.get("payload", {}).get("op") for a in B.chips_for(b, elig)]
    assert "reschedule" not in ops

    assert changes.cancellable_bookings(w.owner, for_reschedule=True) == []
    assert [r["booking_id"] for r in changes.cancellable_bookings(w.owner)] == [w.booking.pk]

    reply = daily.booking_op(w.owner, None, b, "reschedule")
    assert RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE in reply["content"]


def test_assistant_prepare_reschedule_refuses_after_acceptance(egs_factory):
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    prep = booking_mut.prepare_reschedule(user=w.owner, booking_id=w.booking.pk, slot_ids=[w.new_slot.pk])
    assert prep["ok"] is False
    assert prep["error"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED
    assert prep["message"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED_MESSAGE
