"""Razorpay on/off switch, test/live key separation and the test-accounts-only rule."""

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from iic_booking.payments import gateway_mode
from iic_booking.payments.models import PaymentOrder
from iic_booking.payments.models import PaymentPurpose
from iic_booking.payments.razorpay_service import RazorpayNotConfigured
from iic_booking.payments.razorpay_service import RazorpayServiceError
from iic_booking.payments.razorpay_service import create_order
from iic_booking.payments.razorpay_service import get_razorpay_client
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def strict(settings):
    settings.RAZORPAY_ENABLED = True
    settings.RAZORPAY_MODE = "test"
    settings.RAZORPAY_KEY_ID = "rzp_test_unit"
    settings.RAZORPAY_KEY_SECRET = "unit-secret"
    settings.RAZORPAY_LIVE_ALLOWED = False
    settings.RAZORPAY_TEST_MODE_TEST_ACCOUNTS_ONLY = True
    return settings


@pytest.mark.parametrize(
    "changes, expected",
    [
        ({"RAZORPAY_ENABLED": False}, "switched off"),
        ({"RAZORPAY_MODE": "sandbox"}, "must be test or live"),
        ({"RAZORPAY_KEY_SECRET": ""}, "not configured"),
        ({"RAZORPAY_KEY_ID": "rzp_live_abc"}, "does not belong to test mode"),
        ({"RAZORPAY_MODE": "live"}, "does not belong to live mode"),
        ({"RAZORPAY_MODE": "live", "RAZORPAY_KEY_ID": "rzp_live_abc"}, "not been authorised"),
    ],
)
def test_configuration_problems_stop_every_new_payment(strict, changes, expected):
    for name, value in changes.items():
        setattr(strict, name, value)
    assert expected in gateway_mode.configuration_problem()
    with pytest.raises(RazorpayNotConfigured):
        get_razorpay_client()


def test_live_needs_its_own_key_and_the_second_switch(strict):
    strict.RAZORPAY_MODE, strict.RAZORPAY_KEY_ID, strict.RAZORPAY_LIVE_ALLOWED = "live", "rzp_live_abc", True
    assert gateway_mode.configuration_problem() == ""


def test_messages_never_contain_a_key_or_secret(strict):
    strict.RAZORPAY_KEY_ID = "rzp_live_SHOULDNOTSHOW"
    message = gateway_mode.configuration_problem()
    assert "SHOULDNOTSHOW" not in message and "unit-secret" not in message
    assert "SHOULDNOTSHOW" not in str(gateway_mode.public_status())


@pytest.fixture
def recharge_target(db):
    department = Department.objects.create(name="Gateway Test Dept", code="GWT", department_type=DepartmentType.INTERNAL)

    def make(**user_fields):
        user = UserFactory(user_type=UserType.FACULTY, **user_fields)
        wallet, _ = Wallet.objects.get_or_create(user=user)
        return user, wallet, department

    return make


def _fake_client():
    return SimpleNamespace(order=SimpleNamespace(create=lambda data: {"id": f"order_{data['receipt'][-12:]}"}))


@pytest.mark.django_db
def test_test_mode_refuses_real_accounts_and_records_mode_for_test_accounts(strict, recharge_target):
    real, real_wallet, department = recharge_target()
    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=_fake_client()):
        with pytest.raises(RazorpayServiceError, match="Only test accounts"):
            create_order(user=real, purpose=PaymentPurpose.WALLET_RECHARGE, base_amount=Decimal("100"), wallet=real_wallet, department=department)
        tester, tester_wallet, _ = recharge_target(is_test_account=True)
        order, payload = create_order(user=tester, purpose=PaymentPurpose.WALLET_RECHARGE, base_amount=Decimal("100"),
                                      wallet=tester_wallet, department=department)
    assert order.mode == "test" and payload["mode"] == "test"
    assert not PaymentOrder.objects.filter(user=real).exists()


@pytest.mark.django_db
def test_a_test_order_never_settles_for_a_real_account(strict, recharge_target):
    real, wallet, department = recharge_target()
    order = PaymentOrder.objects.create(
        razorpay_order_id="order_testmode1", receipt="r1", purpose=PaymentPurpose.WALLET_RECHARGE, user=real, wallet=wallet,
        department=department, base_amount=Decimal("100"), total_amount=Decimal("100"), idempotency_key="k-testmode1", mode="test",
    )
    with pytest.raises(RazorpayServiceError, match="test-mode payment"):
        settle_order_success(razorpay_order_id=order.razorpay_order_id, razorpay_payment_id="pay_testmode1")
    wallet.refresh_from_db()
    assert wallet.total_balance == 0


@pytest.mark.django_db
def test_create_order_answers_503_when_switched_off(settings, recharge_target):
    settings.RAZORPAY_ENABLED = False
    user, wallet, department = recharge_target()
    client_factory = patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=_fake_client())
    with client_factory, pytest.raises(RazorpayNotConfigured, match="switched off"):
        create_order(user=user, purpose=PaymentPurpose.WALLET_RECHARGE, base_amount=Decimal("100"), wallet=wallet, department=department)
    assert not PaymentOrder.objects.exists()
    with patch("iic_booking.payments.views.create_order", side_effect=RazorpayNotConfigured("Online payment is switched off.")), \
            patch("iic_booking.payments.views.get_object_or_404", return_value=SimpleNamespace(settlement_department=department)):
        client = APIClient()
        client.force_authenticate(user)
        answer = client.post("/api/payments/razorpay/create-order/", {"purpose": "BOOKING_SHORTFALL", "booking_id": 1})
    assert answer.status_code == 503 and answer.json()["code"] == "gateway_unavailable"


@pytest.mark.django_db
def test_fee_settings_tell_the_frontend_the_mode_without_secrets(strict, recharge_target):
    user, _, _ = recharge_target(is_test_account=True)
    client = APIClient()
    client.force_authenticate(user)
    gateway = client.get("/api/payments/fee-settings/").json()["gateway"]
    assert gateway == {"available": True, "mode": "test", "test_mode": True, "reason": ""}
