"""Refunds through the payment gateway are closed: a booking paid partly or wholly through Razorpay is refunded in full
to the wallet, and the manual gateway refund is refused."""

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment import api_views
from iic_booking.payments import razorpay_service
from iic_booking.payments.models import Payment, PaymentOrder, PaymentRefund
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.repositories.wallet_repository import WalletRepository

from .conftest import client_for, make_user

pytestmark = pytest.mark.django_db


def _paid_by_gateway(booking, amount):
    order = PaymentOrder.objects.create(
        razorpay_order_id=f"order_T{uuid.uuid4().hex[:12]}", receipt="test", purpose="BOOKING_SHORTFALL",
        user=booking.user, booking=booking, base_amount=amount, total_amount=amount, currency="INR", status="PAID",
    )
    return Payment.objects.create(payment_order=order, razorpay_payment_id=f"pay_T{uuid.uuid4().hex[:12]}",
                                  amount=amount, status="CAPTURED")


def test_gateway_paid_booking_is_refunded_in_full_to_the_wallet(factory, monkeypatch):
    called = []
    monkeypatch.setattr(razorpay_service, "get_razorpay_client", lambda: called.append("client"))
    user = make_user()
    booking = factory.booking(user, factory.equipment(), factory.at(timezone.localdate() + timedelta(days=5)),
                              total="590.00", wallet_amount_applied=Decimal("90.00"))
    payment = _paid_by_gateway(booking, Decimal("500.00"))
    Wallet.objects.get_or_create(user=user)
    target, _ = WalletRepository.get_booking_wallet_target(user, booking.equipment.internal_department)
    before = Decimal(str(target.balance))

    api_views.refund_booking_internal(booking, "Equipment failure", None)

    target.refresh_from_db()
    booking.refresh_from_db()
    assert booking.status == "REFUNDED"
    assert Decimal(str(target.balance)) - before == Decimal("590.00")  # gateway and wallet portions alike
    assert called == [] and not PaymentRefund.objects.filter(payment=payment).exists()


def test_manual_gateway_refund_is_refused(factory):
    user = make_user()
    booking = factory.booking(user, factory.equipment(), factory.at(timezone.localdate() + timedelta(days=5)))
    payment = _paid_by_gateway(booking, Decimal("100.00"))
    admin = make_user(UserType.ADMIN, is_staff=True, is_superuser=True)
    r = client_for(admin).post("/api/payments/razorpay/refund/", {"payment_pk": payment.pk}, format="json")
    assert r.status_code == 400 and "not supported" in r.json()["error"]
    with pytest.raises(razorpay_service.RazorpayServiceError):
        razorpay_service.create_refund(payment)
