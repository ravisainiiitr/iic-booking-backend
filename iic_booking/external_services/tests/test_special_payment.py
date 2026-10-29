"""Payment deadline, settlement and expiry of special-request allocations (no real payment is made: settlement
functions are called directly with locally created order rows, as the verified webhook would)."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, EquipmentManager, Holiday, SlotStatus, UrgentBookingRequest
from iic_booking.external_services import special_payment as pay
from iic_booking.external_services.models import (
    AllocationState,
    ExternalServicesSettings,
    PolicyText,
    SpecialAllocation,
    SpecialRequestEvent,
)
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_user

pytestmark = pytest.mark.django_db
API = "/api/v1/external-booking/special-requests/"
STAFF = "/api/v1/external-booking/staff/special-requests/"


@pytest.fixture(autouse=True)
def open_portal(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))
    s = ExternalServicesSettings.load()
    s.special_requests_enabled = True
    s.save()


@pytest.fixture
def world(factory):
    eq = factory.equipment()
    oic = make_user(UserType.MANAGER, is_staff=True, department=eq.internal_department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return {"eq": eq, "oic": oic, "user": make_user(), "factory": factory}


def _allocated(world, *, start=None, status=SlotStatus.AVAILABLE, days=20):
    """Submit a request (preferred far ahead: not short notice) and allocate one slot at ``start``."""
    eq, user, factory = world["eq"], world["user"], world["factory"]
    pref = (timezone.localtime() + timedelta(days=days)).replace(hour=10, minute=0, second=0, microsecond=0)
    r = client_for(user).post(API, {
        "equipment_id": eq.pk, "input_values": {"A": 2}, "preferred_start": pref.isoformat(),
        "preferred_end": (pref + timedelta(hours=1)).isoformat(), "sample_description": "Thin film",
    }, format="json")
    assert r.status_code == 201, r.content
    urg_id = r.json()["id"]
    # 14 days ahead: not short notice, so payment needs no acknowledgement unless a test says otherwise.
    start = start or factory.at(timezone.localdate() + timedelta(days=14), 10)
    # An open slot in the same week, so the external weekly quota (a share of bookable slots) is not zero.
    factory.slot(eq, start + timedelta(hours=3))
    slot = factory.slot(eq, start, status=status)
    r = client_for(world["oic"]).post(f"{STAFF}{urg_id}/allocate/", {"slot_ids": [slot.pk]}, format="json")
    assert r.status_code == 200, r.content
    a = SpecialAllocation.objects.get(request_id=urg_id)
    return a, slot


def _order(booking, *, status="CREATED"):
    from iic_booking.payments.models import PaymentOrder

    return PaymentOrder.objects.create(
        razorpay_order_id=f"order_test_{booking.pk}_{timezone.now().timestamp()}", receipt=f"r{booking.pk}",
        purpose="BOOKING_SHORTFALL", user=booking.user, booking=booking, base_amount=booking.amount_due,
        total_amount=booking.amount_due, status=status,
    )


def _settle(booking):
    from django.db import transaction

    from iic_booking.payments.razorpay_service import _settle_booking_shortfall

    with transaction.atomic():
        _settle_booking_shortfall(_order(booking))


def _move_deadline(a, delta):
    SpecialAllocation.objects.filter(pk=a.pk).update(payment_deadline_at=timezone.now() + delta)
    a.refresh_from_db()


# --- deadline -------------------------------------------------------------------------------------------------------

def test_deadline_rule():
    approved = timezone.now()
    assert pay.payment_deadline(approved, approved + timedelta(days=3), window_hours=24) == approved + timedelta(hours=24)
    assert pay.payment_deadline(approved, approved + timedelta(hours=24), window_hours=24) == approved + timedelta(hours=24)
    assert pay.payment_deadline(approved, approved + timedelta(hours=5), window_hours=24) == approved + timedelta(hours=5)


def test_deadline_cannot_be_set_from_the_client(world):
    a, _slot = _allocated(world)
    before = a.payment_deadline_at
    user = world["user"]
    client_for(user).post(f"{API}{a.request_id}/respond/", {"message": "x", "payment_deadline_at": "2099-01-01"},
                          format="json")
    a.refresh_from_db()
    assert a.payment_deadline_at == before


# --- payment start --------------------------------------------------------------------------------------------------

def test_payment_refusal_only_applies_to_special_allocations(world):
    ordinary = world["factory"].booking(make_user(), world["eq"], world["factory"].at(timezone.localdate() + timedelta(days=5)))
    assert pay.payment_refusal(ordinary) is None
    a, _ = _allocated(world)
    assert pay.payment_refusal(a.booking) is None
    _move_deadline(a, -timedelta(minutes=1))
    assert pay.payment_refusal(a.booking)["code"] == "SPECIAL_PAYMENT_DEADLINE_PASSED"


def test_short_notice_allocation_needs_acknowledgement_before_payment(world):
    start = (timezone.now() + timedelta(days=2)).replace(second=0, microsecond=0)
    a, _ = _allocated(world, start=start)
    assert a.short_notice and a.short_notice_ack_at is None
    assert pay.payment_refusal(a.booking)["code"] == "SHORT_NOTICE_ACK_REQUIRED"
    version = PolicyText.objects.filter(key="short_notice").order_by("-version").first().version
    user = world["user"]
    r = client_for(user).post(f"{API}{a.request_id}/acknowledge-short-notice/", {"short_notice_ack_version": 999},
                              format="json")
    assert r.status_code == 400
    r = client_for(user).post(f"{API}{a.request_id}/acknowledge-short-notice/",
                              {"short_notice_ack_version": version}, format="json")
    assert r.status_code == 200, r.content
    a.refresh_from_db()
    assert a.short_notice_ack_at is not None and pay.payment_refusal(a.booking) is None
    other = make_user()
    r = client_for(other).post(f"{API}{a.request_id}/acknowledge-short-notice/",
                               {"short_notice_ack_version": version}, format="json")
    assert r.status_code == 404


def test_razorpay_order_is_refused_after_the_deadline(world):
    from iic_booking.payments.razorpay_service import RazorpayServiceError, create_order

    a, _ = _allocated(world)
    _move_deadline(a, -timedelta(minutes=1))
    with pytest.raises(RazorpayServiceError):
        create_order(user=world["user"], purpose="BOOKING_SHORTFALL", booking=a.booking)


# --- settlement -----------------------------------------------------------------------------------------------------

def test_payment_before_the_deadline_confirms_the_booking(world):
    a, slot = _allocated(world)
    _settle(a.booking)
    a.refresh_from_db()
    booking = Booking.objects.get(pk=a.booking_id)
    assert a.state == AllocationState.PAID and booking.status == BookingStatus.BOOKED
    assert booking.amount_due == Decimal("0.00") and booking.payment_settled_at
    assert SpecialRequestEvent.objects.filter(request_id=a.request_id, action="paid").count() == 1
    assert pay.expire_overdue(timezone.now() + timedelta(days=3)) == {}  # nothing left to expire


def test_gateway_confirmation_within_the_grace_period_is_accepted(world):
    a, _ = _allocated(world)
    _move_deadline(a, -timedelta(minutes=5))
    _settle(a.booking)
    a.refresh_from_db()
    assert a.state == AllocationState.PAID
    assert Booking.objects.get(pk=a.booking_id).status == BookingStatus.BOOKED


def test_payment_after_the_grace_period_does_not_revive_the_booking(world):
    a, slot = _allocated(world)
    _move_deadline(a, -timedelta(minutes=30))
    _settle(a.booking)
    a.refresh_from_db()
    booking = Booking.objects.get(pk=a.booking_id)
    assert a.state == AllocationState.EXPIRED and a.late_payment_at is not None
    assert booking.status == BookingStatus.CANCELLED and booking.payment_settled_at is not None
    slot.refresh_from_db()
    assert slot.booking_id is None and slot.status == SlotStatus.AVAILABLE


def test_late_callback_after_expiry_is_recorded_not_applied(world):
    a, slot = _allocated(world)
    _move_deadline(a, -timedelta(hours=1))
    assert pay.expire_overdue() == {"expired": 1}
    _settle(Booking.objects.get(pk=a.booking_id))
    a.refresh_from_db()
    assert a.state == AllocationState.EXPIRED and a.late_payment_at is not None
    assert Booking.objects.get(pk=a.booking_id).status == BookingStatus.CANCELLED
    assert SpecialRequestEvent.objects.filter(request_id=a.request_id, action="late_payment").exists()


def test_offline_receipt_submitted_before_the_deadline_counts(world):
    from iic_booking.users.models import DepartmentPaymentReceipt

    a, _ = _allocated(world)
    fields = {f.name for f in DepartmentPaymentReceipt._meta.get_fields()}
    if "booking" not in fields:
        pytest.skip("offline receipts have no booking link on this baseline")
    booking = Booking.objects.get(pk=a.booking_id)
    from django.db import transaction

    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(pk=booking.pk)
        assert pay.settlement_hook(locked, realised_at=a.payment_deadline_at - timedelta(minutes=1), source="offline")
    a.refresh_from_db()
    assert a.state == AllocationState.PAID


# --- expiry ---------------------------------------------------------------------------------------------------------

def test_expiry_waits_for_the_deadline_and_grace(world):
    a, _ = _allocated(world)
    assert pay.expire_overdue() == {}
    _move_deadline(a, -timedelta(minutes=10))  # inside the gateway grace
    assert pay.expire_overdue() == {}
    _move_deadline(a, -timedelta(minutes=16))
    assert pay.expire_overdue() == {"expired": 1}


def test_expiry_is_idempotent_and_records_everything(world):
    a, slot = _allocated(world)
    _move_deadline(a, -timedelta(hours=1))
    assert pay.expire_overdue() == {"expired": 1}
    assert pay.expire_overdue() == {}
    assert pay.expire_one(a.pk) == "skipped"
    a.refresh_from_db()
    booking = Booking.objects.get(pk=a.booking_id)
    assert a.state == AllocationState.EXPIRED and booking.status == BookingStatus.CANCELLED
    assert UrgentBookingRequest.all_types.get(pk=a.request_id).status == "EXPIRED"
    assert SpecialRequestEvent.objects.filter(request_id=a.request_id, action="expired").count() == 1
    from iic_booking.equipment.cancellation_models import BookingCancellation

    cancellation = BookingCancellation.objects.filter(booking=booking).first()
    assert cancellation is not None and cancellation.reason == "PAYMENT_DEADLINE_EXPIRED"


def test_a_paid_booking_is_never_expired(world):
    a, _ = _allocated(world)
    Booking.objects.filter(pk=a.booking_id).update(payment_settled_at=timezone.now())
    _move_deadline(a, -timedelta(hours=1))
    assert pay.expire_overdue() == {"paid": 1}
    assert Booking.objects.get(pk=a.booking_id).status == BookingStatus.PENDING_PAYMENT  # left to the settlement
    a.refresh_from_db()
    assert a.state == AllocationState.PAID


def test_a_gateway_order_marked_paid_blocks_expiry(world):
    a, _ = _allocated(world)
    _order(Booking.objects.get(pk=a.booking_id), status="PAID")
    _move_deadline(a, -timedelta(hours=1))
    assert pay.expire_overdue() == {"paid": 1}


@pytest.mark.parametrize("previous, expected", [
    (SlotStatus.AVAILABLE, SlotStatus.AVAILABLE),
    (SlotStatus.NOT_AVAILABLE, SlotStatus.NOT_AVAILABLE),  # weekend / holiday rows stay closed
    (SlotStatus.BLOCKED, SlotStatus.BLOCKED),
    (SlotStatus.SCHEDULED_MAINTENANCE, SlotStatus.SCHEDULED_MAINTENANCE),
])
def test_slot_restoration_keeps_the_previous_state(world, previous, expected):
    a, slot = _allocated(world, status=previous)
    assert a.previous_slot_statuses[str(slot.pk)] == previous
    _move_deadline(a, -timedelta(hours=1))
    pay.expire_overdue()
    slot.refresh_from_db()
    assert slot.booking_id is None and slot.status == expected


def test_restoration_honours_maintenance_started_after_allocation(world):
    a, slot = _allocated(world)
    eq = world["eq"]
    type(eq).objects.filter(pk=eq.pk).update(status="REPAIR")
    _move_deadline(a, -timedelta(hours=1))
    pay.expire_overdue()
    slot.refresh_from_db()
    assert slot.status == SlotStatus.UNDER_MAINTENANCE


def test_restoration_honours_a_holiday_declared_after_allocation(world):
    a, slot = _allocated(world)
    Holiday.objects.create(date=slot.date, reason="Declared later", is_active=True)
    _move_deadline(a, -timedelta(hours=1))
    pay.expire_overdue()
    slot.refresh_from_db()
    assert slot.status == SlotStatus.NOT_AVAILABLE


def test_deadline_is_the_slot_start_when_the_slot_is_sooner_than_the_payment_window(world):
    start = (timezone.localtime() + timedelta(hours=3)).replace(minute=0, second=0, microsecond=0)
    a, slot = _allocated(world, start=start)
    assert slot.start_datetime < a.approved_at + timedelta(hours=24)
    assert a.payment_deadline_at == slot.start_datetime


def test_an_expired_allocation_on_a_closed_slot_does_not_become_bookable(world):
    from .conftest import give_wallet

    a, slot = _allocated(world, status=SlotStatus.NOT_AVAILABLE)  # e.g. a weekend slot the OIC allocated
    _move_deadline(a, -timedelta(hours=1))
    assert pay.expire_overdue() == {"expired": 1}
    slot.refresh_from_db()
    assert slot.status == SlotStatus.NOT_AVAILABLE and slot.booking_id is None
    other = make_user()
    give_wallet(other, world["eq"].internal_department, balance="1000.00")
    r = client_for(other).post(f"/api/equipments/{world['eq'].pk}/book/",
                               {"slot_ids": [slot.pk], "input_values": {}}, format="json")
    assert r.status_code != 201, r.content
    slot.refresh_from_db()
    assert slot.booking_id is None and slot.status == SlotStatus.NOT_AVAILABLE
