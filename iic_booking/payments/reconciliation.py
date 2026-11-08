"""Gateway reconciliation: Razorpay's own records against the portal's.

* ``record_gateway_charges``: keeps Razorpay's fee and tax on each payment, so gross, fee, tax and net stay apart.
* ``sync_settlement_report``: Razorpay's settlement reconciliation report for a day, one ``SettlementLine`` per
  payment, refund or adjustment, linked to the payment and its settlement.
* ``recover_missed_payments``: asks Razorpay about recent unpaid orders. A payment Razorpay confirms as captured for
  the order's exact amount is settled exactly as the webhook would have settled it; anything else is raised.
* ``run_checks``: missing, duplicate and mismatched payments and settlement gaps become payment issues for Finance.
* ``summary``: the figures Finance needs for a date range, never mixing collected money with settled money.

Nothing here edits an existing payment amount, order total or wallet transaction.
"""

from __future__ import annotations

import logging
from datetime import date
from datetime import datetime
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count
from django.db.models import Sum
from django.utils import timezone

from .issues import raise_issue
from .models import Payment
from .models import PaymentIssue
from .models import PaymentIssueKind
from .models import PaymentIssueStatus
from .models import PaymentOrder
from .models import PaymentOrderStatus
from .models import PaymentRefund
from .models import PaymentSettlement
from .models import PaymentStatus
from .models import PaymentVerifiedVia
from .models import RefundStatus
from .models import SettlementLine

logger = logging.getLogger(__name__)
ZERO = Decimal("0.00")


def _rupees(paise) -> Decimal:
    try:
        return (Decimal(str(int(paise or 0))) / 100).quantize(Decimal("0.01"))
    except (TypeError, ValueError, ArithmeticError):
        return ZERO


def _when(timestamp):
    try:
        return datetime.fromtimestamp(int(timestamp), tz=timezone.get_current_timezone()) if timestamp else None
    except (TypeError, ValueError, OverflowError):
        return None


def record_gateway_charges(razorpay_payment_id: str, entity: dict | None) -> None:
    """Store Razorpay's fee and tax for a payment when the entity carries them. Never raises."""
    if not isinstance(entity, dict) or entity.get("fee") is None:
        return
    try:
        Payment.objects.filter(razorpay_payment_id=razorpay_payment_id, gateway_fee__isnull=True).update(
            gateway_fee=_rupees(entity.get("fee")), gateway_tax=_rupees(entity.get("tax"))
        )
    except Exception:
        logger.exception("could not store gateway charges for payment %s", razorpay_payment_id)


def sync_settlement_report(day: date, *, client=None) -> int:
    """Pull Razorpay's settlement report for one day. Repeating a day changes nothing. Returns lines added."""
    from .razorpay_service import get_razorpay_client

    client = client or get_razorpay_client()
    report = client.settlement.report({"year": day.year, "month": day.month, "day": day.day})
    items = (report or {}).get("items") or []
    added = 0
    for item in items:
        if not isinstance(item, dict) or not item.get("entity_id") or not item.get("settlement_id"):
            continue
        with transaction.atomic():
            payment = Payment.objects.filter(razorpay_payment_id=item["entity_id"]).first() if item.get("type") == "payment" else None
            line, created = SettlementLine.objects.get_or_create(
                settlement_id=str(item["settlement_id"])[:64],
                entity_id=str(item["entity_id"])[:64],
                defaults={
                    "settlement_utr": str(item.get("settlement_utr") or "")[:128],
                    "entity_type": str(item.get("type") or "")[:24],
                    "razorpay_order_id": str(item.get("order_id") or "")[:64],
                    "payment": payment,
                    "amount": _rupees(item.get("amount")),
                    "fee": _rupees(item.get("fee")),
                    "tax": _rupees(item.get("tax")),
                    "credit": _rupees(item.get("credit")),
                    "debit": _rupees(item.get("debit")),
                    "settled_at": _when(item.get("settled_at")),
                },
            )
            if not created:
                continue
            added += 1
            settlement, _ = PaymentSettlement.objects.get_or_create(
                settlement_id=line.settlement_id,
                defaults={"bank_utr": line.settlement_utr, "settled_on": line.settled_at.date() if line.settled_at else None},
            )
            if payment is not None:
                settlement.payments.add(payment)
                Payment.objects.filter(pk=payment.pk, gateway_fee__isnull=True).update(gateway_fee=line.fee, gateway_tax=line.tax)
    return added


def recover_missed_payments(*, hours: int = 72, client=None) -> dict:
    """Unpaid orders from the last ``hours`` (older than 15 minutes, created in the current mode): ask Razorpay."""
    from . import gateway_mode
    from .razorpay_service import RazorpayServiceError
    from .razorpay_service import _amount_or_currency_problem
    from .razorpay_service import get_razorpay_client
    from .razorpay_service import settle_order_success

    client = client or get_razorpay_client()
    now = timezone.now()
    orders = PaymentOrder.objects.filter(
        status__in=[PaymentOrderStatus.CREATED, PaymentOrderStatus.FAILED],
        created_at__gte=now - timedelta(hours=hours),
        created_at__lte=now - timedelta(minutes=15),
        mode=gateway_mode.mode(),
    )
    recovered = raised = checked = 0
    for order in orders.iterator():
        checked += 1
        try:
            found = client.order.payments(order.razorpay_order_id)
        except Exception:
            logger.warning("Razorpay order payments fetch failed for %s", order.razorpay_order_id, exc_info=True)
            continue
        for entity in (found or {}).get("items") or []:
            if not isinstance(entity, dict) or entity.get("status") != "captured" or entity.get("order_id") != order.razorpay_order_id:
                continue
            problem = _amount_or_currency_problem(order, entity)
            if problem:
                raise_issue(PaymentIssueKind.MISMATCH, key=entity.get("id"), order=order, razorpay_payment_id=entity.get("id", ""),
                            amount=_rupees(entity.get("amount")), detail=f"Found by reconciliation, not applied: {problem}.")
                raised += 1
                continue
            try:
                settle_order_success(
                    razorpay_order_id=order.razorpay_order_id,
                    razorpay_payment_id=entity["id"],
                    verified_via=PaymentVerifiedVia.RECONCILIATION,
                    raw_payload={"source": "reconciliation", "payment": {k: entity.get(k) for k in ("id", "order_id", "amount", "currency", "status", "method")}},
                    method=str(entity.get("method") or ""),
                )
                record_gateway_charges(entity["id"], entity)
                recovered += 1
            except RazorpayServiceError as exc:
                raise_issue(PaymentIssueKind.NOT_APPLIED, key=entity.get("id"), order=order, razorpay_payment_id=entity.get("id", ""),
                            amount=_rupees(entity.get("amount")), detail=f"Found by reconciliation, not applied: {exc}")
                raised += 1
    return {"orders_checked": checked, "recovered": recovered, "raised": raised}


def run_checks(*, unsettled_after_days: int | None = None) -> dict:
    """Raise an issue for each missing, duplicate or mismatched record. Existing open issues are not duplicated."""
    days = unsettled_after_days if unsettled_after_days is not None else int(getattr(settings, "RAZORPAY_UNSETTLED_ALERT_DAYS", 5))
    counts = {"order_without_payment": 0, "duplicate_capture": 0, "unsettled": 0, "settlement_mismatch": 0, "settlement_unknown": 0}

    for order in PaymentOrder.objects.filter(status=PaymentOrderStatus.PAID).exclude(
        payments__status__in=[PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED]
    ).iterator():
        raise_issue(PaymentIssueKind.ORDER_WITHOUT_PAYMENT, key=order.razorpay_order_id, order=order, amount=order.total_amount,
                    detail="Order is marked paid but holds no captured payment.")
        counts["order_without_payment"] += 1

    for row in (Payment.objects.filter(status=PaymentStatus.CAPTURED).values("payment_order_id")
                .annotate(n=Count("id")).filter(n__gt=1)):
        for payment in Payment.objects.filter(payment_order_id=row["payment_order_id"]).order_by("created_at")[1:]:
            raise_issue(PaymentIssueKind.DUPLICATE_CAPTURE, key=payment.razorpay_payment_id, order=payment.payment_order,
                        razorpay_payment_id=payment.razorpay_payment_id, amount=payment.amount,
                        detail="More than one captured payment on this order.")
            counts["duplicate_capture"] += 1

    cutoff = timezone.now() - timedelta(days=days)
    for payment in Payment.objects.filter(status=PaymentStatus.CAPTURED, created_at__lte=cutoff, settlement_lines__isnull=True) \
            .select_related("payment_order").iterator():
        raise_issue(PaymentIssueKind.UNSETTLED, key=payment.razorpay_payment_id, order=payment.payment_order,
                    razorpay_payment_id=payment.razorpay_payment_id, amount=payment.amount,
                    detail=f"Captured more than {days} days ago and not yet in any Razorpay settlement.")
        counts["unsettled"] += 1

    for line in SettlementLine.objects.filter(entity_type="payment").select_related("payment").iterator():
        if line.payment is None:
            raise_issue(PaymentIssueKind.SETTLEMENT_UNKNOWN, key=line.entity_id, razorpay_order_id=line.razorpay_order_id,
                        razorpay_payment_id=line.entity_id, amount=line.amount,
                        detail=f"Settlement {line.settlement_id} includes a payment the portal has no record of.")
            counts["settlement_unknown"] += 1
        elif line.amount != line.payment.amount:
            raise_issue(PaymentIssueKind.SETTLEMENT_MISMATCH, key=line.entity_id, order=line.payment.payment_order,
                        razorpay_payment_id=line.entity_id, amount=line.amount,
                        detail=f"Settlement {line.settlement_id} shows {line.amount}; the portal recorded {line.payment.amount}.")
            counts["settlement_mismatch"] += 1
    return counts


def reconcile(*, days: int = 7, client=None) -> dict:
    """The daily run: settlement reports for the last ``days`` days, missed-payment recovery, then the checks."""
    from .razorpay_service import get_razorpay_client

    client = client or get_razorpay_client()
    today = timezone.localdate()
    lines = 0
    for offset in range(1, days + 1):
        try:
            lines += sync_settlement_report(today - timedelta(days=offset), client=client)
        except Exception:
            logger.warning("settlement report for %s could not be read", today - timedelta(days=offset), exc_info=True)
    recovery = recover_missed_payments(client=client)
    checks = run_checks()
    return {"settlement_lines_added": lines, **recovery, "checks": checks}


def summary(date_from: date, date_to: date, *, mode: str | None = None) -> dict:
    """Figures for orders created in the range. Gross collected, gateway charges, refunds and settled money are kept
    apart: a settlement is what reached the institute's bank, not what was collected. Test-mode orders are left out
    unless ``mode="test"`` asks for them alone, so test money never mixes with real money."""
    orders = PaymentOrder.objects.filter(created_at__date__gte=date_from, created_at__date__lte=date_to)
    orders = orders.filter(mode="test") if mode == "test" else orders.exclude(mode="test")
    by_status = dict(orders.values_list("status").annotate(n=Count("id")).values_list("status", "n"))
    payments = Payment.objects.filter(payment_order__in=orders, status__in=[
        PaymentStatus.CAPTURED, PaymentStatus.PARTIALLY_REFUNDED, PaymentStatus.REFUNDED])
    money = payments.aggregate(gross=Sum("amount"), fee=Sum("gateway_fee"), tax=Sum("gateway_tax"))
    refunded = PaymentRefund.objects.filter(payment__in=payments, status=RefundStatus.PROCESSED).aggregate(s=Sum("amount"))["s"] or ZERO
    settled = SettlementLine.objects.filter(payment__in=payments, entity_type="payment").aggregate(
        amount=Sum("amount"), fee=Sum("fee"), tax=Sum("tax"), credit=Sum("credit"))
    gross = money["gross"] or ZERO
    return {
        "date_from": date_from.isoformat(),
        "date_to": date_to.isoformat(),
        "orders": {"total": orders.count(), "paid": by_status.get(PaymentOrderStatus.PAID, 0),
                   "pending": by_status.get(PaymentOrderStatus.CREATED, 0), "failed": by_status.get(PaymentOrderStatus.FAILED, 0),
                   "by_mode": dict(orders.values_list("mode").annotate(n=Count("id")).values_list("mode", "n"))},
        "amount_initiated": str(orders.aggregate(s=Sum("total_amount"))["s"] or ZERO),
        "amount_collected": str(gross),
        "amount_refunded_through_gateway": str(refunded),
        "net_collected": str(gross - refunded),
        "gateway_fee": str(money["fee"] or ZERO),
        "gateway_tax": str(money["tax"] or ZERO),
        "settled_gross": str(settled["amount"] or ZERO),
        "settled_to_bank": str(settled["credit"] or ZERO),
        "not_yet_settled": str(gross - (settled["amount"] or ZERO)),
        "open_issues": dict(PaymentIssue.objects.filter(status=PaymentIssueStatus.OPEN).values_list("kind").annotate(n=Count("id"))
                            .values_list("kind", "n")),
        "mode": "test only" if mode == "test" else "live and earlier orders; test-mode orders excluded",
    }
