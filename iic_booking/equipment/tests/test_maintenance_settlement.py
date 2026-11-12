"""OIC / Main Administrator settle bookings on hold for maintenance; a settled booking can no longer be refunded."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import booking_events
from iic_booking.equipment.booking_cancellation import CancellationValidationError, perform_booking_cancellation
from iic_booking.equipment.maintenance_settlement import SETTLED_MARKER, is_refund_locked
from iic_booking.equipment.models import (
    BookingCancellationRequest,
    BookingDisruptionKind,
    BookingEvent,
    BookingStatus,
    EquipmentManager,
    SlotStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _no_dispatch(monkeypatch):
    monkeypatch.setattr(booking_events, "_dispatch_booking_event_notification", lambda *_a, **_k: None)


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def _hold(f, eq=None, *, waiting=True):
    """A booking on maintenance hold (status Awaiting your choice, kind Maintenance), as the policy leaves it."""
    eq = eq or f.equipment(status="REPAIR")
    booking = f.booking(f.student(), eq, f.future(days=2))
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.MAINTENANCE
    booking.disruption_release_slot_status = SlotStatus.UNDER_MAINTENANCE
    booking.maintenance_disruption_flag = True
    booking.maintenance_auto_reschedule_pending = waiting
    booking.save()
    return booking


def _oic(f, eq):
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic


def _settle(user, booking, target="COMPLETED", reason="Analysis was done before the breakdown."):
    return _client(user).post(
        f"/api/bookings/{booking.pk}/maintenance-settle/", {"status": target, "reason": reason}, format="json"
    )


@pytest.fixture
def main_admin(db):
    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


# --- who may see and settle ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_oic_of_equipment_lists_bookings_on_hold_without_refund(egs_factory):
    eq = egs_factory.equipment(status="REPAIR")
    waiting = _hold(egs_factory, eq, waiting=True)
    undecided = _hold(egs_factory, eq, waiting=False)
    refunded = egs_factory.booking(egs_factory.student(), eq, egs_factory.future(days=4))
    refunded.status = BookingStatus.REFUNDED
    refunded.save(update_fields=["status"])
    other_kind = _hold(egs_factory, eq)
    other_kind.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    other_kind.save(update_fields=["disruption_kind"])

    res = _client(_oic(egs_factory, eq)).get(f"/api/equipments/{eq.pk}/maintenance-settlement/")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["under_maintenance"] is True
    assert {r["booking_id"] for r in body["bookings"]} == {waiting.pk, undecided.pk}
    assert [t["value"] for t in body["targets"]] == ["COMPLETED", "BOOKING_NOT_UTILIZED"]


@pytest.mark.django_db
@pytest.mark.parametrize("target", ["COMPLETED", "BOOKING_NOT_UTILIZED"])
def test_oic_settles_hold_and_it_is_locked(egs_factory, target):
    booking = _hold(egs_factory)
    oic = _oic(egs_factory, booking.equipment)
    res = _settle(oic, booking, target)
    assert res.status_code == 200, res.content
    assert res.json()["refund_locked"] is True
    booking.refresh_from_db()
    assert booking.status == target
    assert booking.disruption_kind is None
    assert booking.maintenance_auto_reschedule_pending is False
    event = BookingEvent.objects.filter(booking=booking).order_by("-event_id").first()
    assert event.metadata[SETTLED_MARKER] is True
    assert event.created_by_id == oic.pk
    assert "Officer In-Charge" in event.comment
    assert is_refund_locked(booking)


@pytest.mark.django_db
def test_main_admin_may_settle_any_equipment(egs_factory, main_admin):
    booking = _hold(egs_factory)
    assert _client(main_admin).get(f"/api/equipments/{booking.equipment_id}/maintenance-settlement/").status_code == 200
    res = _settle(main_admin, booking)
    assert res.status_code == 200, res.content
    assert is_refund_locked(booking)


@pytest.mark.django_db
@pytest.mark.parametrize("user_type", [UserType.OPERATOR, UserType.DEPT_ADMIN, UserType.STUDENT, UserType.FACULTY])
def test_other_user_types_cannot_settle(egs_factory, user_type):
    booking = _hold(egs_factory)
    other = UserFactory(user_type=user_type, department=egs_factory.department, admin_approved=True)
    assert _client(other).get(f"/api/equipments/{booking.equipment_id}/maintenance-settlement/").status_code == 403
    assert _settle(other, booking).status_code == 403
    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING
    assert not is_refund_locked(booking)


@pytest.mark.django_db
def test_owner_cannot_settle_own_booking(egs_factory):
    booking = _hold(egs_factory)
    assert _settle(booking.user, booking).status_code == 403


@pytest.mark.django_db
def test_oic_of_other_equipment_cannot_settle(egs_factory):
    booking = _hold(egs_factory)
    other_oic = _oic(egs_factory, egs_factory.equipment())
    assert _client(other_oic).get(f"/api/equipments/{booking.equipment_id}/maintenance-settlement/").status_code == 403
    assert _settle(other_oic, booking).status_code == 403
    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING


@pytest.mark.django_db
def test_only_maintenance_holds_and_final_targets(egs_factory, main_admin):
    booking = _hold(egs_factory)
    assert _settle(main_admin, booking, "REFUNDED").status_code == 400
    assert _settle(main_admin, booking, "BOOKED").status_code == 400
    assert _settle(main_admin, booking, reason="short").status_code == 400

    booked = egs_factory.booking(egs_factory.student(), booking.equipment, egs_factory.future(days=5))
    assert _settle(main_admin, booked).status_code == 400
    booked.refresh_from_db()
    assert booked.status == BookingStatus.BOOKED


# --- refund lock ------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_settled_booking_refused_by_staff_refund_endpoint(egs_factory, main_admin):
    booking = _hold(egs_factory)
    assert _settle(main_admin, booking).status_code == 200
    res = _client(main_admin).post(f"/api/bookings/{booking.pk}/refund/", {"notes": "try"}, format="json")
    assert res.status_code == 400
    assert "can no longer be refunded" in res.json()["error"]
    booking.refresh_from_db()
    assert booking.status == BookingStatus.COMPLETED


@pytest.mark.django_db
def test_settled_booking_refused_by_cancellation_with_refund(egs_factory, main_admin):
    booking = _hold(egs_factory)
    assert _settle(main_admin, booking).status_code == 200
    booking.refresh_from_db()
    with pytest.raises(CancellationValidationError, match="can no longer be refunded"):
        perform_booking_cancellation(
            booking,
            slot_ids=list(booking.daily_slots.values_list("id", flat=True)),
            should_refund=True,
            cancel_notes="",
            actor=main_admin,
            allow_started_slots=True,
        )


@pytest.mark.django_db
def test_settled_booking_refused_by_user_cancel(egs_factory, main_admin):
    booking = _hold(egs_factory)
    assert _settle(main_admin, booking).status_code == 200
    res = _client(booking.user).post(f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json")
    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.status == BookingStatus.COMPLETED


@pytest.mark.django_db
def test_settled_booking_cancellation_request_cannot_be_approved(egs_factory, main_admin):
    booking = _hold(egs_factory)
    assert _settle(main_admin, booking).status_code == 200
    req = BookingCancellationRequest.objects.create(booking=booking, user=booking.user, notes="please refund")
    with pytest.raises(ValueError, match="can no longer be refunded"):
        req.approve("ok")
    booking.refresh_from_db()
    assert booking.status == BookingStatus.COMPLETED


@pytest.mark.django_db
def test_main_admin_status_override_of_maintenance_hold_also_locks_refund(egs_factory, main_admin):
    booking = _hold(egs_factory)
    res = _client(main_admin).post(
        f"/api/bookings/{booking.pk}/status-override/",
        {"status": "COMPLETED", "reason": "Analysis was done before the breakdown."},
        format="json",
    )
    assert res.status_code == 200, res.content
    assert is_refund_locked(booking)


@pytest.mark.django_db
def test_refund_locked_in_booking_serializers(egs_factory, main_admin):
    from iic_booking.equipment.models import Booking
    from iic_booking.equipment.serializers import BookingListSerializer, BookingSerializer

    settled = _hold(egs_factory)
    open_hold = _hold(egs_factory, settled.equipment)
    assert _settle(main_admin, settled).status_code == 200
    assert BookingSerializer(settled, context={"viewer": main_admin}).data["refund_locked"] is True
    assert BookingSerializer(open_hold, context={"viewer": main_admin}).data["refund_locked"] is False
    rows = BookingListSerializer(
        Booking.objects.filter(pk__in=[settled.pk, open_hold.pk]).order_by("pk"), many=True,
        context={"viewer": main_admin},
    ).data
    # The list shows the display reference as booking_id; rows come in pk order.
    assert [r["refund_locked"] for r in rows] == [True, False]


# --- default flow unchanged ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_unsettled_hold_can_still_be_cancelled_for_full_refund(egs_factory):
    booking = _hold(egs_factory)
    assert not is_refund_locked(booking)
    res = _client(booking.user).post(f"/api/bookings/{booking.pk}/user-cancel/", {"refund": True}, format="json")
    assert res.status_code == 200, res.content
    booking.refresh_from_db()
    assert booking.status in (BookingStatus.REFUNDED, BookingStatus.CANCELLED)


@pytest.mark.django_db
def test_override_of_non_maintenance_outcome_does_not_lock_refund(egs_factory, main_admin):
    eq = egs_factory.equipment()
    booking = egs_factory.booking(egs_factory.student(), eq, timezone.now() - timedelta(hours=5))
    booking.status = BookingStatus.DISRUPTION_PENDING
    booking.disruption_kind = BookingDisruptionKind.OPERATOR_ABSENT
    booking.maintenance_disruption_flag = True
    booking.save()
    res = _client(main_admin).post(
        f"/api/bookings/{booking.pk}/status-override/",
        {"status": "COMPLETED", "reason": "Operator was present; analysis completed."},
        format="json",
    )
    assert res.status_code == 200
    assert not is_refund_locked(booking)
    assert booking.total_charge == Decimal("10.00")
