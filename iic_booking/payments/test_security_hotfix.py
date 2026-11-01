"""Security hotfix: legacy wallet Razorpay recharge endpoints and the standalone refund endpoint. No network."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from rest_framework.test import APIClient

from iic_booking.payments.models import (
    Payment,
    PaymentOrder,
    PaymentOrderStatus,
    PaymentPurpose,
    PaymentRefund,
    PaymentStatus,
    RefundStatus,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletRazorpayOrder
from iic_booking.users.repositories.wallet_repository import SubWalletRepository, WalletRepository
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

FAKE_SECRET = "unit-test-secret-not-real"


@pytest.fixture
def secret(settings):
    settings.RAZORPAY_KEY_SECRET = FAKE_SECRET
    return settings


@pytest.fixture
def dept():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(name=f"Hotfix Chemistry {tag}", code=f"HC{tag}", department_type=DepartmentType.INTERNAL)


@pytest.fixture
def faculty(dept):
    return UserFactory(user_type=UserType.FACULTY, email_verified=True, admin_approved=True, department=dept)


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _signature(order_id, payment_id, secret=FAKE_SECRET):
    return hmac.new(secret.encode(), f"{order_id}|{payment_id}".encode(), hashlib.sha256).hexdigest()


def _legacy_order(user, dept, *, paise=100, order_id="order_LEGACY1"):
    wallet, _ = WalletRepository.get_or_create(user)
    WalletRazorpayOrder.objects.create(wallet=wallet, department=dept, amount_paise=paise, order_id=order_id)
    return wallet


def _balance(wallet, dept):
    sub = SubWalletRepository.get_or_create(wallet, dept)
    sub.refresh_from_db()
    return sub.balance


def _verify(user, **data):
    return _client(user).post("/api/wallet/razorpay/verify-payment/", data, format="json")


# --- legacy recharge ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("gateway_on,expected", [(True, 410), (False, 403)])
def test_legacy_create_order_never_creates_an_order(faculty, dept, gateway_on, expected):
    from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

    settings_row = WalletSricSettings.get_singleton()
    settings_row.online_gateway_recharge_enabled = gateway_on
    settings_row.save()
    with patch("razorpay.Client") as client:
        response = _client(faculty).post("/api/wallet/razorpay/create-order/", {"amount": 100, "department_id": dept.pk}, format="json")
    assert response.status_code == expected
    if gateway_on:
        assert "/api/payments/razorpay/create-order/" in response.data["error"]
    client.assert_not_called()
    assert not WalletRazorpayOrder.objects.exists()


def test_legacy_verify_locks_the_order_row(secret, faculty, dept):
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    _legacy_order(faculty, dept)
    with CaptureQueriesContext(connection) as queries:
        _verify(faculty, razorpay_order_id="order_LEGACY1", razorpay_payment_id="pay_1",
                razorpay_signature=_signature("order_LEGACY1", "pay_1"))
    locking = [q["sql"] for q in queries.captured_queries if "FOR UPDATE" in q["sql"] and "walletrazorpayorder" in q["sql"].lower()]
    assert locking, "the legacy order row must be locked before crediting"


def test_legacy_verify_credits_the_stored_amount_not_the_client_amount(secret, faculty, dept):
    wallet = _legacy_order(faculty, dept, paise=100)
    response = _verify(faculty, razorpay_order_id="order_LEGACY1", razorpay_payment_id="pay_1",
                       razorpay_signature=_signature("order_LEGACY1", "pay_1"), amount=10_000_000)
    assert response.status_code == 400
    assert _balance(wallet, dept) == Decimal("0.00")
    assert WalletRazorpayOrder.objects.filter(order_id="order_LEGACY1").exists()  # still settleable correctly


@pytest.mark.parametrize("amount", [None, 100, "100"])
def test_legacy_verify_settles_a_paid_order_once(secret, faculty, dept, amount):
    wallet = _legacy_order(faculty, dept, paise=100)
    data = {"razorpay_order_id": "order_LEGACY1", "razorpay_payment_id": "pay_1",
            "razorpay_signature": _signature("order_LEGACY1", "pay_1")}
    if amount is not None:
        data["amount"] = amount
    assert _verify(faculty, **data).status_code == 200
    assert _balance(wallet, dept) == Decimal("1.00")
    assert _verify(faculty, **data).status_code == 400  # replay
    assert _balance(wallet, dept) == Decimal("1.00")


def test_legacy_verify_rejects_a_bad_signature(secret, faculty, dept):
    wallet = _legacy_order(faculty, dept)
    response = _verify(faculty, razorpay_order_id="order_LEGACY1", razorpay_payment_id="pay_1", razorpay_signature="0" * 64)
    assert response.status_code == 400
    assert _balance(wallet, dept) == Decimal("0.00")


def test_legacy_verify_rejects_another_users_order(secret, faculty, dept):
    _legacy_order(faculty, dept)
    other = UserFactory(user_type=UserType.FACULTY, email_verified=True, admin_approved=True, department=dept)
    response = _verify(other, razorpay_order_id="order_LEGACY1", razorpay_payment_id="pay_1",
                       razorpay_signature=_signature("order_LEGACY1", "pay_1"))
    assert response.status_code == 400
    assert WalletRazorpayOrder.objects.filter(order_id="order_LEGACY1").exists()


# --- standalone refund endpoint -------------------------------------------------------------------------------------

@pytest.fixture
def captured_payment(faculty, dept):
    wallet, _ = WalletRepository.get_or_create(faculty)
    order = PaymentOrder.objects.create(
        razorpay_order_id="order_PAID1", receipt="r1", purpose=PaymentPurpose.WALLET_RECHARGE, user=faculty,
        wallet=wallet, department=dept, base_amount=Decimal("500.00"), total_amount=Decimal("500.00"),
        status=PaymentOrderStatus.PAID, idempotency_key="idem-paid-1",
    )
    return Payment.objects.create(payment_order=order, razorpay_payment_id="pay_PAID1", amount=Decimal("500.00"))


@pytest.mark.parametrize("staff_type", [UserType.OPERATOR, UserType.MANAGER, UserType.DEPT_ADMIN, UserType.FACULTY])
def test_refund_endpoint_refuses_non_finance_roles(captured_payment, staff_type):
    staff = UserFactory(user_type=staff_type, email_verified=True, admin_approved=True)
    with patch("iic_booking.payments.views.create_refund") as create_refund:
        response = _client(staff).post("/api/payments/razorpay/refund/", {"payment_id": "pay_PAID1"}, format="json")
    assert response.status_code == 403
    create_refund.assert_not_called()


@pytest.mark.parametrize("staff_type", [UserType.FINANCE, UserType.ADMIN])
def test_refund_endpoint_allows_finance_and_admin(captured_payment, staff_type):
    staff = UserFactory(user_type=staff_type, email_verified=True, admin_approved=True)
    with patch("iic_booking.payments.views.create_refund") as create_refund:
        create_refund.return_value.razorpay_refund_id = "rfnd_1"
        create_refund.return_value.amount = Decimal("500.00")
        create_refund.return_value.status = "PENDING"
        response = _client(staff).post("/api/payments/razorpay/refund/", {"payment_id": "pay_PAID1"}, format="json")
    assert response.status_code == 200
    create_refund.assert_called_once()


# --- refund accounting (create_refund / refund webhooks) ------------------------------------------------------------

def _gateway_refund(refund_id, status="pending"):
    client = MagicMock()
    client.payment.refund.return_value = {"id": refund_id, "status": status}
    return client


@pytest.fixture
def gateway_refunds_open(monkeypatch):
    # Gateway refunds are closed in production (GATEWAY_REFUNDS_SUPPORTED = False); these tests keep the refund
    # accounting correct for the day they are reopened.
    monkeypatch.setattr("iic_booking.payments.razorpay_service.GATEWAY_REFUNDS_SUPPORTED", True)


def test_pending_full_refund_is_not_requested_twice(captured_payment, gateway_refunds_open):
    from iic_booking.payments.razorpay_service import create_refund

    client = _gateway_refund("rfnd_A")
    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=client):
        first = create_refund(captured_payment, reason="booking cancelled")
        second = create_refund(captured_payment, reason="retry")
    assert first.pk == second.pk
    assert client.payment.refund.call_count == 1
    captured_payment.refresh_from_db()
    assert captured_payment.status == PaymentStatus.CAPTURED  # not REFUNDED until Razorpay processes it


def test_pending_refunds_count_toward_the_limit(captured_payment, gateway_refunds_open):
    from iic_booking.payments.razorpay_service import RazorpayServiceError, create_refund

    client = _gateway_refund("rfnd_B")
    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=client):
        create_refund(captured_payment, amount=Decimal("300.00"))
        with pytest.raises(RazorpayServiceError):
            create_refund(captured_payment, amount=Decimal("300.00"))
    assert client.payment.refund.call_count == 1


def test_full_refund_after_a_partial_one_refunds_only_the_remainder(captured_payment, gateway_refunds_open):
    from iic_booking.payments.razorpay_service import create_refund

    client = MagicMock()
    client.payment.refund.side_effect = [{"id": "rfnd_C1", "status": "processed"}, {"id": "rfnd_C2", "status": "processed"}]
    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=client):
        create_refund(captured_payment, amount=Decimal("200.00"))
        captured_payment.refresh_from_db()
        assert captured_payment.status == PaymentStatus.PARTIALLY_REFUNDED
        create_refund(captured_payment)
    assert client.payment.refund.call_args_list[1].args[1]["amount"] == 30000  # 300.00 in paise
    captured_payment.refresh_from_db()
    assert captured_payment.status == PaymentStatus.REFUNDED


def _refund_webhook(settings, event, refund_id):
    from iic_booking.payments.razorpay_service import handle_webhook

    settings.RAZORPAY_WEBHOOK_SECRET = "unit-test-webhook-secret"
    payload = {"event": event, "payload": {"refund": {"entity": {"id": refund_id}}}}
    body = json.dumps(payload).encode()
    signature = hmac.new(b"unit-test-webhook-secret", body, hashlib.sha256).hexdigest()
    return handle_webhook(payload, body, signature)


def test_refund_webhooks_drive_the_payment_status(settings, captured_payment, gateway_refunds_open):
    from iic_booking.payments.razorpay_service import create_refund

    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=_gateway_refund("rfnd_D")):
        create_refund(captured_payment)
    _refund_webhook(settings, "refund.processed", "rfnd_D")
    captured_payment.refresh_from_db()
    assert captured_payment.status == PaymentStatus.REFUNDED
    _refund_webhook(settings, "refund.failed", "rfnd_D")  # late/replayed failure can't undo a processed refund
    assert PaymentRefund.objects.get(razorpay_refund_id="rfnd_D").status == RefundStatus.PROCESSED
    captured_payment.refresh_from_db()
    assert captured_payment.status == PaymentStatus.REFUNDED


def test_failed_refund_frees_the_amount_for_a_new_refund(settings, captured_payment, gateway_refunds_open):
    from iic_booking.payments.razorpay_service import create_refund

    client = MagicMock()
    client.payment.refund.side_effect = [{"id": "rfnd_E1", "status": "pending"}, {"id": "rfnd_E2", "status": "pending"}]
    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=client):
        create_refund(captured_payment)
        _refund_webhook(settings, "refund.failed", "rfnd_E1")
        retry = create_refund(captured_payment)
    assert retry.razorpay_refund_id == "rfnd_E2"
    captured_payment.refresh_from_db()
    assert captured_payment.status == PaymentStatus.CAPTURED
