"""Once the lab accepts the sample, the booking user can no longer cancel it (and a supervisor never can);
staff (OIC / Admin / Department Admin) still can. A lab-flagged disruption keeps the user's choice."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from iic_booking.equipment.models import Booking, BookingStatus, SampleTraceStatus
from iic_booking.equipment.reschedule_lock import (
    CANCEL_LOCKED_SAMPLE_ACCEPTED,
    CANCEL_LOCKED_SAMPLE_ACCEPTED_MESSAGE,
    CANCEL_OWNER_ONLY,
    RESCHEDULE_LOCKED_SAMPLE_ACCEPTED,
)
from iic_booking.equipment.serializers import BookingListSerializer, BookingSerializer
from iic_booking.equipment.tests.test_reschedule_lock_sample_accepted import _request_for, _setup, _trace
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _user_cancel(egs_factory, user, booking):
    return egs_factory.client_for(user).post(f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json")


def _status(booking):
    return Booking.objects.values_list("status", flat=True).get(pk=booking.pk)


def _cancelled(booking) -> bool:
    return _status(booking) in (BookingStatus.CANCELLED, BookingStatus.REFUNDED)


def test_owner_can_cancel_before_acceptance(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_SENT, SampleTraceStatus.FORWARDED_TO_LAB)

    res = _user_cancel(egs_factory, w.owner, w.booking)

    assert res.status_code == 200, res.data
    assert _cancelled(w.booking)


@pytest.mark.parametrize(
    "statuses",
    [
        (SampleTraceStatus.SAMPLE_SENT, SampleTraceStatus.SAMPLE_ACCEPTED),
        (SampleTraceStatus.SAMPLE_ACCEPTED, SampleTraceStatus.PROCESSING),
        (SampleTraceStatus.RETURNED,),
    ],
)
def test_owner_blocked_after_acceptance(egs_factory, egs_quiet_side_effects, statuses):
    w = _setup(egs_factory)
    _trace(w.booking, *statuses)

    res = _user_cancel(egs_factory, w.owner, w.booking)

    assert res.status_code == 400
    assert res.data == {"error": CANCEL_LOCKED_SAMPLE_ACCEPTED_MESSAGE, "code": CANCEL_LOCKED_SAMPLE_ACCEPTED}
    assert _status(w.booking) == BookingStatus.BOOKED


def test_owner_partial_cancel_preview_blocked_after_acceptance(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    slot_id = w.booking.daily_slots.values_list("id", flat=True).first()

    res = egs_factory.client_for(w.owner).post(
        f"/api/bookings/{w.booking.pk}/partial-cancel-preview/", {"slot_ids": [slot_id]}, format="json"
    )

    assert res.status_code == 400
    assert res.data["code"] == CANCEL_LOCKED_SAMPLE_ACCEPTED


def test_supervisor_cannot_cancel(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    res = _user_cancel(egs_factory, w.faculty, w.booking)

    assert res.status_code == 403
    assert _status(w.booking) == BookingStatus.BOOKED


def test_oic_can_still_cancel_after_acceptance(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    res = egs_factory.client_for(w.oic).post(f"/api/bookings/{w.booking.pk}/cancel/", {"refund": True}, format="json")

    assert res.status_code == 200, res.data
    assert _cancelled(w.booking)


def test_admin_can_still_cancel_after_acceptance(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True)

    res = egs_factory.client_for(admin).post(f"/api/bookings/{w.booking.pk}/cancel/", {"refund": True}, format="json")

    assert res.status_code == 200, res.data
    assert _cancelled(w.booking)


def test_lab_disruption_keeps_users_cancel_choice(egs_factory, egs_quiet_side_effects):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    Booking.objects.filter(pk=w.booking.pk).update(maintenance_disruption_flag=True)
    w.booking.refresh_from_db()

    data = BookingSerializer(w.booking, context={"request": _request_for(w.owner)}).data
    assert data["can_cancel"] is True
    assert data["can_reschedule"] is True

    res = _user_cancel(egs_factory, w.owner, w.booking)
    assert res.status_code == 200, res.data
    assert _cancelled(w.booking)


# --- serializer ---------------------------------------------------------------------------------


def test_serializer_owner_can_cancel_before_acceptance(egs_factory):
    w = _setup(egs_factory)
    data = BookingSerializer(w.booking, context={"request": _request_for(w.owner)}).data
    assert data["can_cancel"] is True
    assert data["cancel_block_reason"] is None
    assert data["cancel_block_message"] is None


def test_serializer_blocks_owner_after_acceptance(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    data = BookingSerializer(w.booking, context={"request": _request_for(w.owner)}).data
    assert data["can_cancel"] is False
    assert data["cancel_block_reason"] == CANCEL_LOCKED_SAMPLE_ACCEPTED
    assert data["cancel_block_message"] == CANCEL_LOCKED_SAMPLE_ACCEPTED_MESSAGE
    assert data["reschedule_block_reason"] == RESCHEDULE_LOCKED_SAMPLE_ACCEPTED


def test_serializer_supervisor_never_gets_cancel(egs_factory):
    w = _setup(egs_factory)
    data = BookingSerializer(w.booking, context={"request": _request_for(w.faculty)}).data
    assert data["can_cancel"] is False
    assert data["cancel_block_reason"] == CANCEL_OWNER_ONLY

    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    data = BookingSerializer(w.booking, context={"request": _request_for(w.faculty)}).data
    assert data["can_cancel"] is False
    assert data["cancel_block_reason"] == CANCEL_LOCKED_SAMPLE_ACCEPTED


def test_serializer_staff_keep_cancel_after_acceptance(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        data = BookingSerializer(w.booking, context={"request": _request_for(w.oic)}).data
    assert data["can_cancel"] is True
    assert data["cancel_block_reason"] is None


def test_serializer_without_request_leaves_can_cancel_unknown(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    data = BookingSerializer(w.booking).data
    assert data["can_cancel"] is None
    assert data["cancel_block_reason"] is None


def test_list_serializer_marks_only_accepted_bookings(egs_factory):
    w = _setup(egs_factory)
    other = egs_factory.booking(w.owner, w.eq, egs_factory.future(days=8, hour=10))
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    qs = Booking.objects.filter(pk__in=[w.booking.pk, other.pk]).order_by("pk")
    rows = {r["real_booking_id"]: r for r in BookingListSerializer(qs, many=True, context={"request": _request_for(w.owner)}).data}
    assert rows[w.booking.pk]["can_cancel"] is False
    assert rows[w.booking.pk]["cancel_block_reason"] == CANCEL_LOCKED_SAMPLE_ACCEPTED
    assert rows[other.pk]["can_cancel"] is True


def test_my_bookings_list_api_exposes_cancel_lock(egs_factory):
    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    res = egs_factory.client_for(w.owner).get("/api/bookings/", {"list_view": "1"})
    assert res.status_code == 200, res.data
    row = next(r for r in res.data["bookings"] if r["real_booking_id"] == w.booking.pk)
    assert row["can_cancel"] is False
    assert row["cancel_block_reason"] == CANCEL_LOCKED_SAMPLE_ACCEPTED


# --- Booking Assistant ----------------------------------------------------------------------------


def test_assistant_does_not_offer_cancel_after_acceptance(egs_factory):
    from iic_booking.research_copilot.services.assistant import bookings as B
    from iic_booking.research_copilot.services.assistant import daily
    from iic_booking.research_copilot.services.intelligence import booking_changes as changes
    from iic_booking.research_copilot.services.intelligence.capabilities import Capabilities

    w = _setup(egs_factory)
    assert B.eligibility(B.owned(w.owner, w.booking.pk))["cancel"] is True
    assert [r["booking_id"] for r in changes.cancellable_bookings(w.owner, for_cancel=True)] == [w.booking.pk]

    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)
    b = B.owned(w.owner, w.booking.pk)
    elig = B.eligibility(b)
    assert elig["cancel"] is False
    assert elig["cancel_locked"] is True
    ops = [a.get("payload", {}).get("op") for a in B.chips_for(b, elig)]
    assert "cancel" not in ops and "reschedule" not in ops
    assert changes.cancellable_bookings(w.owner, for_cancel=True) == []
    assert Capabilities(w.owner).has_self_changeable_bookings is False

    reply = daily.booking_op(w.owner, None, b, "cancel")
    assert CANCEL_LOCKED_SAMPLE_ACCEPTED_MESSAGE in reply["content"]

    detail = B.detail_reply(w.owner, None, b)
    assert "rescheduling and cancellation are no longer available" in detail["content"]
    assert "You can cancel" not in detail["content"]


def test_assistant_cancel_mutations_refuse_after_acceptance(egs_factory):
    from iic_booking.research_copilot.services import tools as tools_svc
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    w = _setup(egs_factory)
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    prep = booking_mut.prepare_cancellation(user=w.owner, booking_id=w.booking.pk)
    assert prep["ok"] is False
    assert prep["error"] == CANCEL_LOCKED_SAMPLE_ACCEPTED
    assert prep["message"] == CANCEL_LOCKED_SAMPLE_ACCEPTED_MESSAGE

    tool = tools_svc._prepare_cancel_booking(arguments={"booking_id": w.booking.pk}, user=w.owner)
    assert tool["ok"] is False
    assert tool["error"] == CANCEL_LOCKED_SAMPLE_ACCEPTED


def test_assistant_execute_cancel_refuses_if_accepted_after_prepare(egs_factory, egs_quiet_side_effects):
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    w = _setup(egs_factory)
    prep = booking_mut.prepare_cancellation(user=w.owner, booking_id=w.booking.pk)
    assert prep["ok"] is True
    _trace(w.booking, SampleTraceStatus.SAMPLE_ACCEPTED)

    with patch.object(booking_mut, "_flag", return_value=True):
        out = booking_mut.execute_booking_cancel(
            user=w.owner, proposal_id=prep["proposal_id"], confirmation_token=prep["confirmation_token"]
        )
    assert out["ok"] is False
    assert out["error"] == CANCEL_LOCKED_SAMPLE_ACCEPTED
    assert _status(w.booking) == BookingStatus.BOOKED
