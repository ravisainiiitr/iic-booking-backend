"""Receipts and the single checkout identity (Razorpay milestone 4)."""

from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from iic_booking.payments.models import PaymentOrder
from iic_booking.payments.models import PaymentPurpose
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
URL = "/api/payments/receipts/{}/"


@pytest.fixture
def paid_recharge():
    department = Department.objects.create(name="Receipt Dept", code="RCP", department_type=DepartmentType.INTERNAL)
    user = UserFactory(user_type=UserType.FACULTY, name="Receipt Payer")
    wallet, _ = Wallet.objects.get_or_create(user=user)
    order = PaymentOrder.objects.create(
        razorpay_order_id="order_RCP1", receipt="iic_wall_1_abc123", purpose=PaymentPurpose.WALLET_RECHARGE, user=user,
        wallet=wallet, department=department, base_amount=Decimal("1000.00"), convenience_fee=Decimal("20.00"),
        fee_gst=Decimal("3.60"), fee_gst_percent_snapshot=Decimal("18"), total_amount=Decimal("1023.60"),
        idempotency_key="k-rcp1", mode="test",
    )
    settle_order_success(razorpay_order_id="order_RCP1", razorpay_payment_id="pay_RCP1")
    return user


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_payer_gets_a_receipt_with_the_existing_reference_and_breakdown(paid_recharge):
    data = _client(paid_recharge).get(URL.format("pay_RCP1")).json()
    assert data["receipt_number"] == "iic_wall_1_abc123"
    assert (data["base_amount"], data["convenience_fee"], data["gst_on_fee"], data["total_paid"]) == ("1000.00", "20.00", "3.60", "1023.60")
    assert data["collected_by"] == "DEAN SRIC IIT ROORKEE" and data["institution"] == "Indian Institute of Technology Roorkee"
    assert data["test_mode"] is True and "not a GST tax invoice" in data["not_a_tax_invoice"]
    assert data["description"].startswith("Wallet recharge")


def test_other_users_get_404_and_finance_can_read(paid_recharge):
    assert _client(UserFactory()).get(URL.format("pay_RCP1")).status_code == 404
    assert _client(UserFactory()).get(URL.format("pay_NOPE")).status_code == 404
    assert _client(UserFactory(user_type=UserType.FINANCE)).get(URL.format("pay_RCP1")).status_code == 200


def test_checkout_name_is_one_setting(settings, paid_recharge):
    settings.RAZORPAY_CHECKOUT_NAME = "Name On The Razorpay Account"
    assert _client(paid_recharge).get(URL.format("pay_RCP1")).json()["collected_by"] == "Name On The Razorpay Account"
    assert _client(paid_recharge).get("/api/payments/fee-settings/").json()["gateway"]["checkout_name"] == "Name On The Razorpay Account"


def test_receipt_carries_the_bank_reference_and_settlement_utr(paid_recharge):
    from iic_booking.payments.models import Payment, SettlementLine

    payment = Payment.objects.get(razorpay_payment_id="pay_RCP1")
    Payment.objects.filter(pk=payment.pk).update(gateway_reference="RRN123456789")
    SettlementLine.objects.create(settlement_id="setl_R", settlement_utr="UTR98765", entity_id="pay_RCP1", entity_type="payment",
                                  payment=payment, amount=Decimal("1023.60"))
    data = _client(paid_recharge).get(URL.format("pay_RCP1")).json()
    assert (data["bank_reference"], data["settlement_utr"]) == ("RRN123456789", "UTR98765")


def test_my_receipts_lists_only_my_confirmed_payments(paid_recharge):
    mine = _client(paid_recharge).get("/api/payments/receipts/").json()["results"]
    assert [r["razorpay_payment_id"] for r in mine] == ["pay_RCP1"]
    assert mine[0]["purpose"].startswith("Wallet recharge") and mine[0]["test_mode"] is True
    assert _client(UserFactory()).get("/api/payments/receipts/").json()["results"] == []
