"""Payment issues: money that reached Razorpay but was not applied normally, kept for Finance to act on.

``raise_issue`` never raises: a failure to record is logged, and the payment flow carries on as before. One open
issue per kind and key, so a retried webhook does not open a second one.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.db import IntegrityError
from django.db import transaction

from .models import PaymentIssue
from .models import PaymentIssueStatus

logger = logging.getLogger(__name__)

# The only payment-entity fields kept on our records. Email, phone, card, VPA and bank details are never stored.
_KEEP = ("id", "order_id", "amount", "currency", "status", "method", "captured", "fee", "tax", "created_at",
         "error_code", "error_reason", "refund_status", "amount_refunded")


def minimal_entity(entity: dict | None) -> dict:
    entity = entity if isinstance(entity, dict) else {}
    kept = {k: entity[k] for k in _KEEP if k in entity}
    rrn = (entity.get("acquirer_data") or {}).get("rrn") if isinstance(entity.get("acquirer_data"), dict) else None
    if rrn:
        kept["rrn"] = rrn
    return kept


def minimal_webhook_payload(payload: dict | None) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    inner = payload.get("payload") or {}
    kept = {"event": payload.get("event", ""), "created_at": payload.get("created_at")}
    for name in ("payment", "refund", "order"):
        entity = ((inner.get(name) or {}).get("entity")) if isinstance(inner.get(name), dict) else None
        if entity:
            kept[name] = minimal_entity(entity)
    return kept


def raise_issue(kind: str, *, key: str, detail: str, order=None, razorpay_order_id: str = "", razorpay_payment_id: str = "",
                amount=None) -> PaymentIssue | None:
    key = str(key or razorpay_payment_id or razorpay_order_id or "unknown")[:160]
    try:
        with transaction.atomic():
            existing = PaymentIssue.objects.filter(kind=kind, key=key, status=PaymentIssueStatus.OPEN).first()
            if existing:
                return existing
            issue = PaymentIssue.objects.create(
                kind=kind,
                key=key,
                payment_order=order,
                razorpay_order_id=(razorpay_order_id or getattr(order, "razorpay_order_id", "") or "")[:64],
                razorpay_payment_id=(razorpay_payment_id or "")[:64],
                amount=Decimal(str(amount)) if amount not in (None, "") else None,
                mode=(getattr(order, "mode", "") or "")[:8],
                detail=str(detail)[:500],
            )
    except IntegrityError:
        return PaymentIssue.objects.filter(kind=kind, key=key, status=PaymentIssueStatus.OPEN).first()
    except Exception:
        logger.exception("payment issue %s for %s could not be recorded", kind, key)
        return None
    logger.warning("payment issue raised: %s %s", kind, key)
    return issue
