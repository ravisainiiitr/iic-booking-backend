"""Razorpay payment events must match their order before they settle anything (``_webhook_amount_problem``), and the
existing settlement behaviour (idempotency, ownership, late callbacks, internal bookings and wallet recharges) holds.

No gateway is involved: order rows are created locally, as ``create_order`` stores them, and events are signed with a
test webhook secret.
"""

import hashlib
import hmac
import json
import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.test import Client
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.external_services.models import AllocationState
from iic_booking.payments.models import Payment, PaymentOrder
from iic_booking.users.models import SubWallet, Wallet
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, make_user
from .test_special_payment import _allocated, _move_deadline

pytestmark = pytest.mark.django_db
WEBHOOK = "/api/payments/razorpay/webhook/"
SECRET = "test-webhook-secret"


@pytest.fixture(autouse=True)
def webhook_secret(settings, monkeypatch):
    from iic_booking.external_services.models import ExternalServicesSettings
    from iic_booking.users.legacy_ledger import booking_lock

    settings.RAZORPAY_WEBHOOK_SECRET = SECRET
    settings.RAZORPAY_KEY_SECRET = "test-checkout-secret"
    # the portal-migration lock is not under test (same approach as test_special_payment)
    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))
    s = ExternalServicesSettings.load()
    s.special_requests_enabled = True
    s.save()


@pytest.fixture
def world(factory):
    from iic_booking.equipment.models import EquipmentManager

    eq = factory.equipment()
    oic = make_user(UserType.MANAGER, is_staff=True, department=eq.internal_department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return {"eq": eq, "oic": oic, "user": make_user(), "factory": factory}


def _sign(body: bytes, secret=SECRET) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _post(payload, signature=None):
    body = json.dumps(payload).encode()
    return Client().post(WEBHOOK, data=body, content_type="application/json",
                         HTTP_X_RAZORPAY_SIGNATURE=signature if signature is not None else _sign(body))


def _order(user, *, booking=None, purpose="BOOKING_SHORTFALL", base="590.00", total=None, currency="INR", **extra):
    base = Decimal(base)
    return PaymentOrder.objects.create(
        razorpay_order_id=f"order_T{uuid.uuid4().hex[:12]}", receipt="test", purpose=purpose, user=user,
        booking=booking, base_amount=base, total_amount=Decimal(total) if total else base, currency=currency,
        status="CREATED", **extra,
    )


def _event(order, payment_id=None, **override):
    entity = {"id": payment_id or f"pay_T{uuid.uuid4().hex[:12]}", "order_id": order.razorpay_order_id,
              "amount": int(order.total_amount * 100), "currency": order.currency, "status": "captured",
              "method": "upi"}
    entity.update(override)
    for k in [k for k, v in entity.items() if v is None]:
        del entity[k]
    return {"event": "payment.captured", "payload": {"payment": {"entity": entity}}}


@pytest.fixture
def internal_booking(factory):
    """An internal user's booking awaiting a gateway top-up (the pre-existing shortfall flow)."""
    eq = factory.equipment()
    student = make_user(UserType.STUDENT, department=factory.department)
    start = factory.at(timezone.localdate() + timedelta(days=5), 10)
    return factory.booking(student, eq, start, status=BookingStatus.PENDING_PAYMENT, total="590.00",
                           amount_due=Decimal("590.00"))


def _unchanged(order, booking):
    order.refresh_from_db()
    booking.refresh_from_db()
    return (order.status == "CREATED" and not Payment.objects.filter(payment_order=order).exists()
            and booking.status == BookingStatus.PENDING_PAYMENT and booking.payment_settled_at is None)


# --- amount and currency ---------------------------------------------------------------------------------------------

def test_matching_event_settles_an_internal_booking_shortfall(internal_booking):
    order = _order(internal_booking.user, booking=internal_booking)
    r = _post(_event(order))
    assert r.status_code == 200, r.content
    internal_booking.refresh_from_db()
    order.refresh_from_db()
    assert order.status == "PAID" and internal_booking.status == BookingStatus.BOOKED
    assert Payment.objects.get(payment_order=order).amount == order.total_amount


@pytest.mark.parametrize("override", [
    {"amount": 100}, {"amount": 58999}, {"amount": 59001}, {"amount": None}, {"amount": "not-a-number"},
])
def test_mismatched_or_missing_amount_is_rejected_and_changes_nothing(internal_booking, override):
    order = _order(internal_booking.user, booking=internal_booking)
    r = _post(_event(order, **override))
    assert r.status_code == 400 and "does not match the order" in r.json()["error"]
    assert _unchanged(order, internal_booking)


@pytest.mark.parametrize("currency", ["USD", "", None])
def test_mismatched_or_missing_currency_is_rejected(internal_booking, currency):
    order = _order(internal_booking.user, booking=internal_booking)
    r = _post(_event(order, currency=currency))
    assert r.status_code == 400
    assert _unchanged(order, internal_booking)


def test_currency_comparison_ignores_case(internal_booking):
    order = _order(internal_booking.user, booking=internal_booking)
    assert _post(_event(order, currency="inr")).status_code == 200


# --- duplicates, signatures, unknown and wrong orders --------------------------------------------------------------

def test_duplicate_events_settle_once(internal_booking):
    order = _order(internal_booking.user, booking=internal_booking)
    event = _event(order, payment_id="pay_Tdup0001")
    assert _post(event).status_code == 200
    assert _post(event).status_code == 200
    assert Payment.objects.filter(payment_order=order).count() == 1
    # a second, different capture for an order that is already paid changes nothing (existing behaviour)
    assert _post(_event(order, payment_id="pay_Tdup0002")).status_code == 200
    order.refresh_from_db()
    assert order.status == "PAID" and Payment.objects.filter(payment_order=order).count() == 1


def test_replay_for_a_paid_order_without_amount_stays_a_no_op(internal_booking):
    order = _order(internal_booking.user, booking=internal_booking)
    assert _post(_event(order, payment_id="pay_Treplay01")).status_code == 200
    assert _post(_event(order, payment_id="pay_Treplay01", amount=None, currency=None)).status_code == 200
    assert Payment.objects.filter(payment_order=order).count() == 1


def test_invalid_signature_is_rejected(internal_booking):
    order = _order(internal_booking.user, booking=internal_booking)
    body = json.dumps(_event(order)).encode()
    for signature in ("", "0" * 64, _sign(body, secret="another-secret")):
        r = Client().post(WEBHOOK, data=body, content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE=signature)
        assert r.status_code == 400
    assert _unchanged(order, internal_booking)


def test_unknown_order_is_rejected():
    r = _post({"event": "payment.captured", "payload": {"payment": {"entity": {
        "id": "pay_Tunknown1", "order_id": "order_Tdoesnotexist", "amount": 100, "currency": "INR",
        "status": "captured"}}}})
    assert r.status_code == 400 and not Payment.objects.filter(razorpay_payment_id="pay_Tunknown1").exists()


def test_event_settles_only_its_own_order_whatever_its_notes_say(internal_booking, factory):
    other = factory.booking(internal_booking.user, internal_booking.equipment,
                            factory.at(timezone.localdate() + timedelta(days=6), 11),
                            status=BookingStatus.PENDING_PAYMENT, total="590.00", amount_due=Decimal("590.00"))
    order = _order(internal_booking.user, booking=internal_booking)
    assert _post(_event(order, notes={"booking_id": str(other.pk)})).status_code == 200
    other.refresh_from_db()
    internal_booking.refresh_from_db()
    assert internal_booking.status == BookingStatus.BOOKED and other.status == BookingStatus.PENDING_PAYMENT


def test_checkout_verify_by_another_user_is_forbidden(internal_booking):
    order = _order(internal_booking.user, booking=internal_booking)
    payment_id = "pay_Tcheckout1"
    signature = hmac.new(b"test-checkout-secret", f"{order.razorpay_order_id}|{payment_id}".encode(),
                         hashlib.sha256).hexdigest()
    body = {"razorpay_order_id": order.razorpay_order_id, "razorpay_payment_id": payment_id,
            "razorpay_signature": signature}
    r = client_for(make_user()).post("/api/payments/razorpay/verify/", body, format="json")
    assert r.status_code == 403
    assert _unchanged(order, internal_booking)
    r = client_for(internal_booking.user).post("/api/payments/razorpay/verify/", body, format="json")
    assert r.status_code == 200, r.content


# --- late callbacks for external special allocations ---------------------------------------------------------------

def _special_order(world):
    a, slot = _allocated(world)
    booking = Booking.objects.get(pk=a.booking_id)
    return a, slot, booking, _order(booking.user, booking=booking, base=str(booking.amount_due))


def test_callback_within_the_grace_period_confirms_the_special_booking(world):
    a, _slot, booking, order = _special_order(world)
    _move_deadline(a, -timedelta(minutes=10))
    assert _post(_event(order)).status_code == 200
    a.refresh_from_db()
    booking.refresh_from_db()
    assert a.state == AllocationState.PAID and booking.status == BookingStatus.BOOKED


def test_late_callback_is_recorded_and_does_not_revive_the_booking(world):
    a, slot, booking, order = _special_order(world)
    _move_deadline(a, -timedelta(minutes=20))
    assert _post(_event(order)).status_code == 200
    a.refresh_from_db()
    booking.refresh_from_db()
    slot.refresh_from_db()
    assert a.state == AllocationState.EXPIRED and a.late_payment_at is not None
    assert booking.status == BookingStatus.CANCELLED and slot.booking_id is None
    assert Payment.objects.filter(payment_order=order).count() == 1  # the money is on record for Finance


def test_late_callback_with_a_wrong_amount_is_rejected_before_anything_changes(world):
    a, _slot, booking, order = _special_order(world)
    _move_deadline(a, -timedelta(minutes=20))
    assert _post(_event(order, amount=100)).status_code == 400
    a.refresh_from_db()
    assert a.state == AllocationState.AWAITING_PAYMENT and a.late_payment_at is None


# --- internal wallet recharge ----------------------------------------------------------------------------------------

def test_wallet_recharge_event_still_credits_the_internal_wallet(factory):
    faculty = make_user(UserType.FACULTY, department=factory.department)
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    order = _order(faculty, purpose="WALLET_RECHARGE", base="1000.00", total="1023.60", wallet=wallet,
                   department=factory.department)
    assert _post(_event(order, amount=99999)).status_code == 400  # wrong amount: nothing credited
    assert not SubWallet.objects.filter(wallet=wallet, department=factory.department, balance__gt=0).exists()
    assert _post(_event(order)).status_code == 200
    sub = SubWallet.objects.get(wallet=wallet, department=factory.department)
    assert sub.balance == Decimal("1000.00")  # base amount only; the convenience fee is not credited
    assert _post(_event(order)).status_code == 200  # replay: no second credit
    sub.refresh_from_db()
    assert sub.balance == Decimal("1000.00")
