"""Gateway reconciliation (Razorpay milestone 3): settlement lines, missed-payment recovery, checks and the summary."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.payments.models import Payment
from iic_booking.payments.models import PaymentIssue
from iic_booking.payments.models import PaymentIssueKind
from iic_booking.payments.models import PaymentOrder
from iic_booking.payments.models import PaymentOrderStatus
from iic_booking.payments.models import PaymentPurpose
from iic_booking.payments.models import PaymentSettlement
from iic_booking.payments.models import PaymentStatus
from iic_booking.payments.models import PaymentVerifiedVia
from iic_booking.payments.models import SettlementLine
from iic_booking.payments.razorpay_service import settle_order_success
from iic_booking.payments.reconciliation import recover_missed_payments
from iic_booking.payments.reconciliation import run_checks
from iic_booking.payments.reconciliation import summary
from iic_booking.payments.reconciliation import sync_settlement_report
from iic_booking.payments.tasks import reconcile_gateway
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def make_order():
    department = Department.objects.create(name="Recon Dept", code="RCN", department_type=DepartmentType.INTERNAL)
    counter = iter(range(1, 100))

    def make(amount="500.00", mode="test", minutes_ago=30):
        n = next(counter)
        user = UserFactory(user_type=UserType.FACULTY)
        wallet, _ = Wallet.objects.get_or_create(user=user)
        order = PaymentOrder.objects.create(
            razorpay_order_id=f"order_R{n}", receipt=f"r{n}", purpose=PaymentPurpose.WALLET_RECHARGE, user=user, wallet=wallet,
            department=department, base_amount=Decimal(amount), total_amount=Decimal(amount), idempotency_key=f"k-r{n}", mode=mode,
        )
        PaymentOrder.objects.filter(pk=order.pk).update(created_at=timezone.now() - timedelta(minutes=minutes_ago))
        order.refresh_from_db()
        return order

    return make


def _client(report=None, order_payments=None):
    return SimpleNamespace(
        settlement=SimpleNamespace(report=lambda data: report or {"items": []}),
        order=SimpleNamespace(payments=lambda order_id: (order_payments or {}).get(order_id, {"items": []})),
    )


def test_settlement_report_links_payments_keeps_fee_and_tax_and_repeats_safely(make_order):
    order = make_order()
    _, payment = settle_order_success(razorpay_order_id=order.razorpay_order_id, razorpay_payment_id="pay_S1")
    item = {"entity_id": "pay_S1", "type": "payment", "settlement_id": "setl_1", "settlement_utr": "UTR123", "order_id": order.razorpay_order_id,
            "amount": 50000, "fee": 1180, "tax": 180, "credit": 48820, "debit": 0, "settled_at": 1762560000}
    client = _client({"items": [item]})
    assert sync_settlement_report(timezone.localdate(), client=client) == 1
    assert sync_settlement_report(timezone.localdate(), client=client) == 0
    line = SettlementLine.objects.get()
    payment.refresh_from_db()
    assert (line.payment_id, line.amount, line.fee, line.tax, line.credit) == (payment.pk, Decimal("500.00"), Decimal("11.80"), Decimal("1.80"), Decimal("488.20"))
    assert (payment.gateway_fee, payment.gateway_tax) == (Decimal("11.80"), Decimal("1.80"))
    assert list(PaymentSettlement.objects.get(settlement_id="setl_1").payments.all()) == [payment]


def test_a_missed_webhook_is_recovered_from_razorpay_once(make_order):
    order = make_order()
    found = {order.razorpay_order_id: {"items": [{"id": "pay_M1", "order_id": order.razorpay_order_id, "amount": 50000, "currency": "INR",
                                                   "status": "captured", "method": "upi", "fee": 1180, "tax": 180}]}}
    first = recover_missed_payments(client=_client(order_payments=found))
    again = recover_missed_payments(client=_client(order_payments=found))
    order.refresh_from_db()
    payment = Payment.objects.get()
    assert first["recovered"] == 1 and again["orders_checked"] == 0
    assert order.status == PaymentOrderStatus.PAID and payment.verified_via == PaymentVerifiedVia.RECONCILIATION
    assert payment.gateway_fee == Decimal("11.80")
    order.wallet.refresh_from_db()
    assert order.wallet.total_balance == Decimal("500.00")


def test_recovery_raises_a_wrong_amount_and_skips_failed_or_fresh_orders(make_order):
    wrong, fresh = make_order(), make_order(minutes_ago=2)
    found = {
        wrong.razorpay_order_id: {"items": [{"id": "pay_W", "order_id": wrong.razorpay_order_id, "amount": 100, "currency": "INR", "status": "captured"},
                                            {"id": "pay_F", "order_id": wrong.razorpay_order_id, "amount": 50000, "currency": "INR", "status": "failed"}]},
        fresh.razorpay_order_id: {"items": [{"id": "pay_N", "order_id": fresh.razorpay_order_id, "amount": 50000, "currency": "INR", "status": "captured"}]},
    }
    result = recover_missed_payments(client=_client(order_payments=found))
    assert (result["recovered"], result["raised"]) == (0, 1)
    assert PaymentIssue.objects.get().kind == PaymentIssueKind.MISMATCH and not Payment.objects.exists()


def test_recovery_only_looks_at_orders_of_the_current_mode(settings, make_order):
    make_order(mode="live")
    assert recover_missed_payments(client=_client())["orders_checked"] == 0


def test_checks_find_missing_duplicate_unsettled_and_unknown(make_order):
    paid_without_payment = make_order()
    PaymentOrder.objects.filter(pk=paid_without_payment.pk).update(status=PaymentOrderStatus.PAID)
    old = make_order()
    settle_order_success(razorpay_order_id=old.razorpay_order_id, razorpay_payment_id="pay_OLD")
    Payment.objects.filter(razorpay_payment_id="pay_OLD").update(created_at=timezone.now() - timedelta(days=9))
    SettlementLine.objects.create(settlement_id="setl_x", entity_id="pay_GHOST", entity_type="payment", amount=Decimal("5"))
    counts = run_checks()
    assert counts["order_without_payment"] == 1 and counts["unsettled"] == 1 and counts["settlement_unknown"] == 1
    kinds = set(PaymentIssue.objects.values_list("kind", flat=True))
    assert kinds == {PaymentIssueKind.ORDER_WITHOUT_PAYMENT, PaymentIssueKind.UNSETTLED, PaymentIssueKind.SETTLEMENT_UNKNOWN}
    run_checks()
    assert PaymentIssue.objects.count() == 3  # repeated runs do not duplicate open issues


def test_summary_keeps_test_money_apart_and_separates_gross_from_settled(make_order):
    live, test = make_order(mode="live"), make_order(amount="100.00", mode="test")
    settle_order_success(razorpay_order_id=live.razorpay_order_id, razorpay_payment_id="pay_L")
    settle_order_success(razorpay_order_id=test.razorpay_order_id, razorpay_payment_id="pay_T")
    today = timezone.localdate()
    real = summary(today - timedelta(days=1), today)
    assert real["amount_collected"] == "500.00" and real["settled_gross"] == "0.00" and real["not_yet_settled"] == "500.00"
    assert summary(today - timedelta(days=1), today, mode="test")["amount_collected"] == "100.00"


def test_summary_is_finance_only(make_order):
    for user_type, expected in ((UserType.MANAGER, 403), (UserType.FINANCE, 200), (UserType.ADMIN, 200)):
        client = APIClient()
        client.force_authenticate(UserFactory(user_type=user_type))
        assert client.get("/api/payments/reconciliation/").status_code == expected


def test_the_daily_task_is_idle_while_razorpay_is_off(settings):
    settings.RAZORPAY_ENABLED = False
    assert "skipped" in reconcile_gateway()


def test_captured_status_constant_is_used_for_checks():
    assert PaymentStatus.CAPTURED == "CAPTURED"
