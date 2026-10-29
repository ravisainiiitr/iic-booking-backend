"""External self-booking checkout: payment choice before the booking exists, slots held for the paying user for 15
minutes (or until paid), the booking created (and the wallet debited) only when the payment is confirmed, with slots,
price and wallet re-checked then.

No gateway is called: Razorpay order creation is replaced by a local fake and payments arrive as signed webhooks.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, DailySlot
from iic_booking.external_services.models import CheckoutState, ExternalBookingCheckout
from iic_booking.payments.models import Payment, PaymentOrder, PaymentOrderStatus
from iic_booking.users.models import SubWallet
from iic_booking.users.models.user_type import UserType

from .conftest import client_for, enable_window, give_wallet, make_user
from .test_payment_webhook_integrity import SECRET, _event, _post

pytestmark = pytest.mark.django_db
CHECKOUTS = "/api/v1/external-booking/checkouts/"


class _FakeOrders:
    def create(self, data):
        return {"id": f"order_T{uuid.uuid4().hex[:12]}", "amount": data["amount"], "currency": data["currency"]}


class _FakeClient:
    order = _FakeOrders()


@pytest.fixture(autouse=True)
def local_gateway(settings, monkeypatch):
    from iic_booking.payments import razorpay_service
    from iic_booking.users.legacy_ledger import booking_lock

    settings.RAZORPAY_WEBHOOK_SECRET = SECRET
    settings.RAZORPAY_KEY_ID = "rzp_test_LOCALONLY"
    settings.RAZORPAY_KEY_SECRET = "test-checkout-secret"
    monkeypatch.setattr(razorpay_service, "get_razorpay_client", lambda: _FakeClient())
    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


@pytest.fixture
def world(factory):
    config = enable_window()
    day = config.first_week_start + timedelta(days=2)
    eq = factory.equipment()
    slots = [factory.slot(eq, factory.at(day, h)) for h in (9, 10, 11)]
    return {"eq": eq, "slots": slots, "dept": factory.department, "factory": factory}


def _user(world, balance="0.00", user_type=UserType.EXTERNAL):
    user = make_user(user_type)
    give_wallet(user, world["dept"], balance)
    return user


def _balance(user, world) -> Decimal:
    return SubWallet.objects.get(wallet__user=user, department=world["dept"]).balance


def _start(user, world, slot=None):
    slot = slot or world["slots"][0]
    return client_for(user).post(
        CHECKOUTS, {"equipment_id": world["eq"].pk, "slot_ids": [slot.id], "input_values": {}}, format="json"
    )


def _pay(user, checkout_id, choice):
    return client_for(user).post(f"{CHECKOUTS}{checkout_id}/pay/", {"choice": choice}, format="json")


def _choices(body):
    return [o["choice"] for o in body["options"]]


def _slot_free(slot) -> bool:
    slot.refresh_from_db()
    return slot.booking_id is None and slot.status == "AVAILABLE"


def _total(world, user) -> Decimal:
    r = _start(user, world)
    assert r.status_code == 201, r.content
    return Decimal(r.json()["total_amount"])


# --- nothing is held before payment ---------------------------------------------------------------------------------

def test_direct_external_booking_asks_for_a_payment_choice_and_holds_nothing(world):
    user = _user(world, "5000.00")
    slot = world["slots"][0]
    r = client_for(user).post(f"/api/equipments/{world['eq'].pk}/book/", {"slot_ids": [slot.id], "input_values": {}},
                              format="json")
    assert r.status_code == 409 and r.json()["code"] == "external_checkout_required", r.content
    assert _slot_free(slot) and not Booking.objects.filter(user=user).exists()
    assert _balance(user, world) == Decimal("5000.00")


def test_a_quote_holds_nothing_and_choosing_online_payment_holds_the_slot(world):
    user = _user(world)
    r = _start(user, world)
    assert r.status_code == 201, r.content
    body = r.json()
    assert body["state"] == CheckoutState.QUOTED and body["slot_held"] is False
    r = _pay(user, body["id"], "full_gateway")
    assert r.status_code == 200, r.content
    assert r.json()["checkout"]["state"] == CheckoutState.AWAITING_PAYMENT
    assert r.json()["checkout"]["slot_held"] is True
    # Held, not booked: the slot row is untouched until the payment confirms the booking.
    assert _slot_free(world["slots"][0]) and not Booking.objects.filter(user=user).exists()


def test_a_held_slot_is_refused_to_everyone_else_until_the_hold_lapses(world):
    user = _user(world)
    checkout, _order = _open(user, world, "full_gateway")
    other = _user(world, "15000.00")
    competitor = _start(other, world)
    assert competitor.status_code in (400, 409) and "held" in competitor.content.decode()
    internal = _faculty(world, "15000.00")
    r = client_for(internal).post(f"/api/equipments/{world['eq'].pk}/book/",
                                  {"slot_ids": [world["slots"][0].id], "input_values": {}}, format="json")
    assert r.status_code in (400, 409) and not Booking.objects.filter(user=internal).exists()
    ExternalBookingCheckout.objects.filter(pk=checkout.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    assert _start(other, world).status_code == 201  # the hold lapsed: the slot is open again


def test_two_users_cannot_hold_the_same_slot(world):
    a, b = _user(world), _user(world)
    ca, cb = _start(a, world).json(), _start(b, world).json()  # both quoted before either chose: nothing held yet
    assert _pay(a, ca["id"], "full_gateway").status_code == 200
    r = _pay(b, cb["id"], "full_gateway")
    assert r.status_code in (400, 409) and "held" in r.json()["error"], r.content


def _faculty(world, balance):
    from iic_booking.equipment.models import ChargeProfile, EquipmentProfileType

    ChargeProfile.objects.get_or_create(
        equipment=world["eq"], user_type=UserType.FACULTY,
        defaults={"profile_type": EquipmentProfileType.HOUR, "time_formula": "60", "primary_unit_charge": Decimal("80")},
    )
    return _user(world, balance, user_type=UserType.FACULTY)


def test_internal_user_booking_is_unchanged(world):
    faculty = _faculty(world, "5000.00")
    slot = world["slots"][1]
    r = client_for(faculty).post(f"/api/equipments/{world['eq'].pk}/book/", {"slot_ids": [slot.id], "input_values": {}},
                                 format="json")
    assert r.status_code == 201, r.content
    booking = Booking.objects.get(pk=r.json()["real_booking_id"])
    assert booking.status == BookingStatus.BOOKED and booking.amount_due == 0
    assert _balance(faculty, world) == Decimal("5000.00") - booking.total_charge
    assert not ExternalBookingCheckout.objects.exists()


def test_internal_user_with_enough_wallet_cannot_use_the_checkout(world):
    assert _start(_faculty(world, "5000.00"), world).status_code == 403


def test_internal_user_short_of_funds_is_offered_and_can_pay_online(world):
    faculty = _faculty(world, "1.00")
    slot = world["slots"][1]
    r = client_for(faculty).post(f"/api/equipments/{world['eq'].pk}/book/", {"slot_ids": [slot.id], "input_values": {}},
                                 format="json")
    assert r.status_code == 400 and r.json().get("gateway_checkout_available") is True, r.content
    body = _start(faculty, world, slot=slot).json()
    assert _choices(body) == ["full_gateway"]  # the whole booking online; the wallet is left alone
    r = _pay(faculty, body["id"], "full_gateway")
    assert r.status_code == 200, r.content
    checkout = ExternalBookingCheckout.objects.get(public_id=body["id"])
    assert _post(_event(checkout.payment_order)).status_code == 200
    checkout.refresh_from_db()
    assert checkout.state == CheckoutState.CONFIRMED
    assert checkout.booking.status == BookingStatus.BOOKED and checkout.booking.amount_due == 0
    assert _balance(faculty, world) == Decimal("1.00")


# --- options by wallet balance --------------------------------------------------------------------------------------

def test_zero_wallet_offers_online_payment_only(world):
    body = _start(_user(world), world).json()
    assert _choices(body) == ["full_gateway"]
    assert body["options"][0]["wallet_amount"] == "0.00"
    assert body["options"][0]["gateway_amount"] == body["total_amount"]


def test_partial_wallet_offers_full_online_or_wallet_plus_online(world):
    user = _user(world, "50.00")
    body = _start(user, world).json()
    total = Decimal(body["total_amount"])
    assert total > Decimal("50.00")
    assert _choices(body) == ["full_gateway", "wallet_plus_gateway"]
    full, split = body["options"]
    assert (full["wallet_amount"], Decimal(full["gateway_amount"])) == ("0.00", total)
    assert full["wallet_after"] == "50.00"  # the balance stays unchanged with full online payment
    assert split["wallet_amount"] == "50.00" and Decimal(split["gateway_amount"]) == total - Decimal("50.00")
    assert split["wallet_after"] == "0.00"


def test_sufficient_wallet_offers_full_online_or_full_wallet(world):
    user = _user(world, "15000.00")
    body = _start(user, world).json()
    total = Decimal(body["total_amount"])
    assert _choices(body) == ["full_gateway", "full_wallet"]
    wallet_opt = body["options"][1]
    assert Decimal(wallet_opt["wallet_amount"]) == total and wallet_opt["gateway_amount"] == "0.00"
    assert Decimal(wallet_opt["wallet_after"]) == Decimal("15000.00") - total


# --- wallet-only ----------------------------------------------------------------------------------------------------

def test_full_wallet_confirms_the_booking_and_debits_once(world):
    user = _user(world, "15000.00")
    body = _start(user, world).json()
    total = Decimal(body["total_amount"])
    r = _pay(user, body["id"], "full_wallet")
    assert r.status_code == 200, r.content
    out = r.json()
    assert out["order"] is None and out["checkout"]["state"] == CheckoutState.CONFIRMED
    booking = Booking.objects.get(pk=out["checkout"]["booking"]["id"])
    assert booking.status == BookingStatus.BOOKED and booking.wallet_amount_applied == total and booking.amount_due == 0
    assert _balance(user, world) == Decimal("15000.00") - total
    assert not PaymentOrder.objects.filter(user=user).exists()  # no gateway order for wallet-only payment
    slot = world["slots"][0]
    slot.refresh_from_db()
    assert slot.booking_id == booking.pk
    # Repeated request: same booking, no second debit.
    assert _pay(user, body["id"], "full_wallet").status_code == 200
    assert Booking.objects.filter(user=user).count() == 1 and _balance(user, world) == Decimal("15000.00") - total


def test_wallet_equal_to_the_total_ends_at_zero(world):
    probe = _user(world)
    total = _total(world, probe)
    user = _user(world, str(total))
    body = _start(user, world).json()
    assert "full_wallet" in _choices(body)
    assert _pay(user, body["id"], "full_wallet").status_code == 200
    assert _balance(user, world) == Decimal("0.00")


def test_wallet_only_for_a_slot_taken_meanwhile_is_refused_and_debits_nothing(world):
    user = _user(world, "15000.00")
    body = _start(user, world).json()
    world["factory"].booking(_user(world), world["eq"], world["slots"][0].start_datetime + timedelta(days=1))
    DailySlot.objects.filter(pk=world["slots"][0].pk).update(status="BOOKED")  # someone else took the slot
    r = _pay(user, body["id"], "full_wallet")
    assert r.status_code in (400, 409), r.content
    assert _balance(user, world) == Decimal("15000.00") and not Booking.objects.filter(user=user).exists()
    assert ExternalBookingCheckout.objects.get(public_id=body["id"]).state == CheckoutState.QUOTED


def test_option_not_matching_the_balance_is_refused(world):
    user = _user(world, "50.00")
    body = _start(user, world).json()
    r = _pay(user, body["id"], "full_wallet")
    assert r.status_code == 409 and r.json()["code"] == "option_unavailable"
    assert _pay(user, body["id"], "anything").status_code == 400
    assert _balance(user, world) == Decimal("50.00")


# --- online payment -------------------------------------------------------------------------------------------------

def _open(user, world, choice):
    body = _start(user, world).json()
    r = _pay(user, body["id"], choice)
    assert r.status_code == 200, r.content
    checkout = ExternalBookingCheckout.objects.get(public_id=body["id"])
    return checkout, checkout.payment_order


def test_full_online_payment_confirms_on_capture_without_touching_the_wallet(world):
    user = _user(world, "50.00")
    checkout, order = _open(user, world, "full_gateway")
    assert order.base_amount == checkout.total_amount and order.booking_id is None
    assert _balance(user, world) == Decimal("50.00")
    r = _post(_event(order))
    assert r.status_code == 200, r.content
    checkout.refresh_from_db()
    order.refresh_from_db()
    assert checkout.state == CheckoutState.CONFIRMED and order.status == PaymentOrderStatus.PAID
    booking = checkout.booking
    assert order.booking_id == booking.pk  # cancellation refunds use the existing booking-payment lookup
    assert booking.status == BookingStatus.BOOKED and booking.wallet_amount_applied == 0 and booking.amount_due == 0
    assert booking.payment_settled_at is not None
    assert _balance(user, world) == Decimal("50.00")


def test_wallet_plus_online_charges_the_remainder_and_debits_the_wallet_on_capture(world):
    user = _user(world, "50.00")
    checkout, order = _open(user, world, "wallet_plus_gateway")
    assert checkout.wallet_amount == Decimal("50.00")
    assert order.base_amount == checkout.total_amount - Decimal("50.00")
    assert _balance(user, world) == Decimal("50.00")  # nothing debited before payment
    assert _post(_event(order)).status_code == 200
    checkout.refresh_from_db()
    assert checkout.state == CheckoutState.CONFIRMED
    assert checkout.booking.wallet_amount_applied == Decimal("50.00")
    assert _balance(user, world) == Decimal("0.00")


def test_gateway_amount_is_checked_against_the_online_part_not_the_total(world):
    user = _user(world, "50.00")
    checkout, order = _open(user, world, "wallet_plus_gateway")
    full_total_paise = int(checkout.total_amount * 100)
    r = _post(_event(order, amount=full_total_paise))
    assert r.status_code == 400
    checkout.refresh_from_db()
    assert checkout.state == CheckoutState.AWAITING_PAYMENT and not Payment.objects.filter(payment_order=order).exists()
    assert _balance(user, world) == Decimal("50.00")


def test_duplicate_capture_events_confirm_once(world):
    user = _user(world)
    checkout, order = _open(user, world, "full_gateway")
    event = _event(order, payment_id="pay_Tdupcheckout1")
    assert _post(event).status_code == 200
    assert _post(event).status_code == 200
    assert Booking.objects.filter(user=user).count() == 1
    assert Payment.objects.filter(payment_order=order).count() == 1


def test_payment_reported_after_the_hold_lapsed_and_the_slot_was_taken_is_recorded_and_not_confirmed(world):
    user = _user(world, "50.00")
    checkout, order = _open(user, world, "wallet_plus_gateway")
    ExternalBookingCheckout.objects.filter(pk=checkout.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    other = _user(world, "15000.00")
    competitor = _start(other, world).json()
    assert _pay(other, competitor["id"], "full_wallet").status_code == 200  # the hold lapsed; the other user books
    assert _post(_event(order)).status_code == 200  # the captured payment is accepted and recorded
    checkout.refresh_from_db()
    order.refresh_from_db()
    assert checkout.state == CheckoutState.PAID_NOT_CONFIRMED and checkout.outcome_note
    assert Payment.objects.filter(payment_order=order).count() == 1 and order.status == PaymentOrderStatus.PAID
    assert not Booking.objects.filter(user=user).exists()
    assert _balance(user, world) == Decimal("50.00")  # the wallet part was not taken


def test_payment_after_maintenance_was_declared_is_recorded_and_not_confirmed(world):
    user = _user(world)
    checkout, order = _open(user, world, "full_gateway")
    world["eq"].status = "UNDER_MAINTENANCE"
    world["eq"].save(update_fields=["status"])
    assert _post(_event(order)).status_code == 200
    checkout.refresh_from_db()
    assert checkout.state == CheckoutState.PAID_NOT_CONFIRMED
    assert not Booking.objects.filter(user=user).exists()


def test_wallet_spent_elsewhere_before_capture_is_not_debited_twice(world):
    user = _user(world, "50.00")
    checkout, order = _open(user, world, "wallet_plus_gateway")
    SubWallet.objects.filter(wallet__user=user, department=world["dept"]).update(balance=Decimal("10.00"))
    assert _post(_event(order)).status_code == 200
    checkout.refresh_from_db()
    assert checkout.state == CheckoutState.PAID_NOT_CONFIRMED
    assert _balance(user, world) == Decimal("10.00")


def test_late_capture_after_expiry_still_confirms_while_the_slot_is_free(world):
    user = _user(world)
    checkout, order = _open(user, world, "full_gateway")
    ExternalBookingCheckout.objects.filter(pk=checkout.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
    from iic_booking.external_services.checkout import expire_overdue

    assert expire_overdue() == 1
    assert _post(_event(order)).status_code == 200
    checkout.refresh_from_db()
    assert checkout.state == CheckoutState.CONFIRMED


# --- wallet reservation, expiry, cancel, ownership ------------------------------------------------------------------

def test_wallet_part_awaiting_payment_is_not_offered_twice(world):
    user = _user(world, "50.00")
    first, _order = _open(user, world, "wallet_plus_gateway")
    second = _start(user, world, slot=world["slots"][1]).json()
    assert _choices(second) == ["full_gateway"] and second["wallet_balance"] == "0.00"
    ExternalBookingCheckout.objects.filter(pk=first.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    again = client_for(user).get(f"{CHECKOUTS}{second['id']}/").json()
    assert again["wallet_balance"] == "50.00"  # an expired checkout no longer counts against the wallet


def test_cancelled_or_expired_checkout_cannot_be_paid(world):
    user = _user(world, "15000.00")
    body = _start(user, world).json()
    assert client_for(user).post(f"{CHECKOUTS}{body['id']}/cancel/").json()["state"] == CheckoutState.CANCELLED
    r = _pay(user, body["id"], "full_wallet")
    assert r.status_code == 409 and r.json()["code"] == "checkout_closed"
    assert _balance(user, world) == Decimal("15000.00")


def test_another_user_cannot_see_or_pay_a_checkout(world):
    owner = _user(world, "15000.00")
    body = _start(owner, world).json()
    stranger = _user(world, "15000.00")
    assert client_for(stranger).get(f"{CHECKOUTS}{body['id']}/").status_code == 404
    assert _pay(stranger, body["id"], "full_wallet").status_code == 404
    assert not Booking.objects.exists()


def test_same_choice_retried_reuses_the_open_order(world):
    user = _user(world)
    checkout, order = _open(user, world, "full_gateway")
    r = _pay(user, checkout.public_id, "full_gateway")
    assert r.status_code == 200 and r.json()["order"]["order_id"] == order.razorpay_order_id
    assert PaymentOrder.objects.filter(user=user).count() == 1
    assert _pay(user, checkout.public_id, "full_wallet").status_code == 409


def test_last_slot_goes_to_the_first_confirmed_payment(world):
    a, b = _user(world, "15000.00"), _user(world, "15000.00")
    ca, cb = _start(a, world).json(), _start(b, world).json()
    assert _pay(a, ca["id"], "full_wallet").status_code == 200
    assert _pay(b, cb["id"], "full_wallet").status_code in (400, 409)
    assert Booking.objects.filter(user=b).count() == 0 and _balance(b, world) == Decimal("15000.00")
