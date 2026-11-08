"""Webhook event log, replay protection and the payment issue queue (Razorpay milestone 2)."""

import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from iic_booking.payments import throttles
from iic_booking.payments.models import Payment
from iic_booking.payments.models import PaymentIssue
from iic_booking.payments.models import PaymentIssueKind
from iic_booking.payments.models import PaymentOrder
from iic_booking.payments.models import PaymentOrderStatus
from iic_booking.payments.models import PaymentPurpose
from iic_booking.payments.models import RazorpayWebhookEvent
from iic_booking.payments.models import WebhookEventStatus
from iic_booking.payments.razorpay_service import RazorpayServiceError
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
SECRET = "unit-webhook-secret"
URL = "/api/payments/razorpay/webhook/"


@pytest.fixture(autouse=True)
def _webhook_secret(settings):
    settings.RAZORPAY_WEBHOOK_SECRET = SECRET
    cache.clear()


@pytest.fixture
def recharge_order():
    department = Department.objects.create(name="Webhook Dept", code="WHK", department_type=DepartmentType.INTERNAL)
    user = UserFactory(user_type=UserType.FACULTY)
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return PaymentOrder.objects.create(
        razorpay_order_id="order_WH1", receipt="r-wh1", purpose=PaymentPurpose.WALLET_RECHARGE, user=user, wallet=wallet,
        department=department, base_amount=Decimal("500.00"), total_amount=Decimal("500.00"), idempotency_key="k-wh1",
    )


def _event(kind="payment.captured", order_id="order_WH1", payment_id="pay_WH1", amount=50000, **extra):
    entity = {"id": payment_id, "order_id": order_id, "amount": amount, "currency": "INR", "status": "captured",
              "method": "upi", "email": "payer@example.com", "contact": "+910000000000", "vpa": "someone@upi", **extra}
    return {"event": kind, "payload": {"payment": {"entity": entity}}, "created_at": 1}


def _post(payload, *, event_id="evt_1", secret=SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return APIClient().post(URL, data=body, content_type="application/json", HTTP_X_RAZORPAY_SIGNATURE=signature,
                            HTTP_X_RAZORPAY_EVENT_ID=event_id)


def _wallet_total(order):
    order.wallet.refresh_from_db()
    return order.wallet.total_balance


def test_the_same_event_is_applied_once(recharge_order):
    assert _post(_event()).status_code == 200
    second = _post(_event())
    assert second.status_code == 200 and second.json()["duplicate"] is True
    event = RazorpayWebhookEvent.objects.get(event_id="evt_1")
    assert (event.status, event.attempts, event.razorpay_payment_id) == (WebhookEventStatus.PROCESSED, 2, "pay_WH1")
    assert _wallet_total(recharge_order) == Decimal("500.00")


def test_a_new_event_id_for_the_same_payment_still_credits_once(recharge_order):
    _post(_event(), event_id="evt_a")
    _post(_event(), event_id="evt_b")
    assert Payment.objects.filter(razorpay_payment_id="pay_WH1").count() == 1
    assert _wallet_total(recharge_order) == Decimal("500.00")


def test_a_late_failed_event_does_not_undo_a_paid_order(recharge_order):
    _post(_event(), event_id="evt_ok")
    _post(_event("payment.failed", status="failed"), event_id="evt_late")
    recharge_order.refresh_from_db()
    assert recharge_order.status == PaymentOrderStatus.PAID


def test_an_unsigned_or_wrongly_signed_event_is_refused_and_not_stored(recharge_order):
    assert _post(_event(), secret="not-the-secret").status_code == 400
    assert not RazorpayWebhookEvent.objects.exists() and _wallet_total(recharge_order) == 0


def test_only_minimal_payment_details_are_stored(recharge_order):
    _post(_event())
    stored = json.dumps(Payment.objects.get().raw_payload)
    assert "payer@example.com" not in stored and "+910000000000" not in stored and "someone@upi" not in stored
    assert "pay_WH1" in stored


def test_a_signed_event_that_cannot_be_applied_answers_200_and_is_raised(recharge_order):
    with patch("iic_booking.payments.razorpay_service._settle_wallet_recharge", side_effect=RazorpayServiceError("Wallet missing")):
        answer = _post(_event())
    assert answer.status_code == 200
    issue = PaymentIssue.objects.get()
    assert (issue.kind, issue.razorpay_payment_id, issue.amount) == (PaymentIssueKind.NOT_APPLIED, "pay_WH1", Decimal("500.00"))
    assert RazorpayWebhookEvent.objects.get().status == WebhookEventStatus.IGNORED
    recharge_order.refresh_from_db()
    assert recharge_order.status == PaymentOrderStatus.CREATED and _wallet_total(recharge_order) == 0


def test_an_unexpected_error_answers_500_and_the_retry_applies_it(recharge_order):
    with patch("iic_booking.payments.razorpay_service._settle_wallet_recharge", side_effect=RuntimeError("database blip")):
        assert _post(_event()).status_code == 500
    assert RazorpayWebhookEvent.objects.get().status == WebhookEventStatus.FAILED
    assert _post(_event()).status_code == 200
    event = RazorpayWebhookEvent.objects.get()
    assert (event.status, event.attempts) == (WebhookEventStatus.PROCESSED, 2)
    assert _wallet_total(recharge_order) == Decimal("500.00")


def test_wrong_amount_is_raised_not_applied(recharge_order):
    _post(_event(amount=100))
    assert PaymentIssue.objects.get().kind == PaymentIssueKind.MISMATCH
    assert _wallet_total(recharge_order) == 0


def test_payment_for_an_unknown_order_is_raised():
    _post(_event(order_id="order_NOTOURS", payment_id="pay_STRAY"))
    issue = PaymentIssue.objects.get()
    assert (issue.kind, issue.razorpay_order_id) == (PaymentIssueKind.UNKNOWN_ORDER, "order_NOTOURS")


def test_a_second_capture_on_a_paid_order_is_recorded_and_raised_once(recharge_order):
    settle_order_success(razorpay_order_id="order_WH1", razorpay_payment_id="pay_first")
    settle_order_success(razorpay_order_id="order_WH1", razorpay_payment_id="pay_second")
    settle_order_success(razorpay_order_id="order_WH1", razorpay_payment_id="pay_second")
    assert set(Payment.objects.values_list("razorpay_payment_id", flat=True)) == {"pay_first", "pay_second"}
    issue = PaymentIssue.objects.get()
    assert (issue.kind, issue.key) == (PaymentIssueKind.DUPLICATE_CAPTURE, "pay_second")
    assert _wallet_total(recharge_order) == Decimal("500.00")


def _client_for(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def test_issues_are_finance_only_and_resolving_needs_a_note(recharge_order):
    _post(_event(order_id="order_NOTOURS", payment_id="pay_STRAY"))
    issue = PaymentIssue.objects.get()
    assert _client_for(UserFactory(user_type=UserType.MANAGER)).get("/api/payments/issues/").status_code == 403
    finance = _client_for(UserFactory(user_type=UserType.FINANCE))
    assert finance.get("/api/payments/issues/").json()["results"][0]["razorpay_payment_id"] == "pay_STRAY"
    assert finance.post(f"/api/payments/issues/{issue.pk}/resolve/", {"note": ""}).status_code == 400
    done = finance.post(f"/api/payments/issues/{issue.pk}/resolve/", {"note": "Refunded by NEFT"})
    assert done.json()["status"] == "RESOLVED"


def test_payment_routes_are_rate_limited(monkeypatch, recharge_order):
    monkeypatch.setattr(throttles.PaymentUserThrottle, "THROTTLE_RATES", {"payments_user": "2/min"})
    client = _client_for(recharge_order.user)
    codes = [client.post("/api/payments/razorpay/verify/", {}).status_code for _ in range(3)]
    assert codes[:2] == [400, 400] and codes[2] == 429


class _RacingClient:
    """Razorpay captured the payment itself (automatic capture) just as the portal tried to: capture fails, fetch says captured."""

    def __init__(self, status_after="captured"):
        self.calls = 0
        self.status_after = status_after
        self.payment = self

    def fetch(self, payment_id):
        self.calls += 1
        status = "authorized" if self.calls == 1 else self.status_after
        return {"id": payment_id, "order_id": "order_WH1", "amount": 50000, "currency": "INR", "status": status, "method": "card"}

    def capture(self, *args, **kwargs):
        raise RuntimeError("This payment has already been captured")


def test_an_authorised_event_racing_automatic_capture_raises_no_issue(recharge_order):
    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=_RacingClient()):
        assert _post(_event("payment.authorized", status="authorized"), event_id="evt_auth").status_code == 200
    assert not PaymentIssue.objects.exists()
    assert _wallet_total(recharge_order) == Decimal("500.00")


def test_a_capture_issue_closes_itself_when_the_capture_arrives(recharge_order):
    with patch("iic_booking.payments.razorpay_service.get_razorpay_client", return_value=_RacingClient(status_after="authorized")):
        _post(_event("payment.authorized", status="authorized"), event_id="evt_auth2")
    issue = PaymentIssue.objects.get()
    assert issue.kind == PaymentIssueKind.NOT_CAPTURED and issue.status == "OPEN"
    _post(_event("payment.captured"), event_id="evt_cap2")
    issue.refresh_from_db()
    assert issue.status == "RESOLVED" and "captured" in issue.resolution_note
    assert _wallet_total(recharge_order) == Decimal("500.00")
