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
    from django.utils import timezone

    assert data["receipt_number"] == f"IITR/RCP/{timezone.localtime().year}/TEST-00001"
    assert data["gateway_receipt"] == "iic_wall_1_abc123"
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


UPI_ENTITY = {
    "id": "pay_RCP1", "order_id": "order_RCP1", "method": "upi", "vpa": "receiptpayer@okhdfcbank",
    "acquirer_data": {"rrn": "531234567890", "upi_transaction_id": "AXL0123456789"},
    "email": "someone@example.com", "contact": "+919999999999",
}


def test_upi_receipt_names_the_rrn_transaction_id_and_masked_upi_id(paid_recharge):
    from iic_booking.payments.models import Payment
    from iic_booking.payments.reconciliation import record_gateway_charges

    record_gateway_charges("pay_RCP1", UPI_ENTITY)
    stored = Payment.objects.get(razorpay_payment_id="pay_RCP1").raw_payload["instrument"]
    assert "someone@example.com" not in str(stored) and "9999999999" not in str(stored)
    data = _client(paid_recharge).get(URL.format("pay_RCP1")).json()
    assert data["payment_mode"] == "UPI"
    details = dict(data["payment_details"])
    assert details["UPI reference no. (RRN)"] == "531234567890"
    assert details["UPI transaction ID"] == "AXL0123456789"
    assert details["Payer UPI ID"] == "re**********@okhdfcbank"
    assert details["Payer bank / app"] == "HDFC Bank (Google Pay)"
    assert "does not require a signature" in data["computer_generated"]
    assert data["institute_gstin"] == "05AAALI0033R1Z5"


def test_card_netbanking_and_wallet_details():
    from iic_booking.payments.instrument import extract

    card = extract({"method": "card", "card": {"network": "Visa", "type": "debit", "issuer": "HDFC", "last4": "1007",
                                               "number": "4100280000001007"}, "acquirer_data": {"auth_code": "123456"}})
    assert card == {"method": "card", "card_network": "Visa", "card_type": "debit", "card_issuer": "HDFC",
                    "card_last4": "1007", "auth_code": "123456"}
    nb = extract({"method": "netbanking", "bank": "SBIN", "acquirer_data": {"bank_transaction_id": "BT998877"}})
    assert nb == {"method": "netbanking", "bank": "SBIN", "bank_transaction_id": "BT998877"}
    assert extract({"method": "wallet", "wallet": "paytm"}) == {"method": "wallet", "wallet": "paytm"}


def test_card_receipt_lines(paid_recharge):
    from iic_booking.payments.instrument import describe
    from iic_booking.payments.models import Payment
    from iic_booking.payments.reconciliation import record_gateway_charges

    record_gateway_charges("pay_RCP1", {"method": "card", "card": {"network": "Visa", "type": "credit", "issuer": "ICIC",
                                                                    "last4": "1111"}, "acquirer_data": {"auth_code": "A1B2C3", "rrn": "998877665544"}})
    how = describe(Payment.objects.get(razorpay_payment_id="pay_RCP1"))
    assert how["mode"] == "Credit card (Visa)"
    assert dict(how["details"]) == {"Card number": "XXXX XXXX XXXX 1111", "Issuing bank": "ICICI Bank",
                                    "Authorisation code": "A1B2C3", "Bank reference no. (RRN)": "998877665544"}


def test_without_razorpay_details_the_stored_reference_is_used(paid_recharge):
    from iic_booking.payments.instrument import describe
    from iic_booking.payments.models import Payment

    Payment.objects.filter(razorpay_payment_id="pay_RCP1").update(method="netbanking", gateway_reference="NB123")
    how = describe(Payment.objects.get(razorpay_payment_id="pay_RCP1"))
    assert how == {"mode": "Net banking", "details": [["Bank transaction reference", "NB123"]]}


def test_issuing_bank_codes_print_as_bank_names():
    """Razorpay reports the card issuer as a code ("DCBL" on IICPXRD [B]202600026's receipt)."""
    from iic_booking.payments.instrument import _bank

    assert _bank("DCBL") == "DCB Bank" and _bank("dcbl") == "DCB Bank"
    assert _bank("ZZZZ") == "ZZZZ"  # unknown codes still show as they came
