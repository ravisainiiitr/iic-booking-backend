"""READ-ONLY Razorpay test-mode check. Every query runs in a read-only database transaction; nothing is written.

Run logs are public, so the result is printed only encrypted to the public key passed in (CHECK_PUBLIC_KEY), as
one line starting CIPHERTEXT. Optional BOOKING_REFS (comma-separated booking references or IDs) narrows the detail.
No names, emails, phone numbers or secrets are collected: users appear only as IDs with their test-account flag.
"""

import base64
import json
import os
import sys
from decimal import Decimal

sys.path.insert(0, os.getcwd())
import django  # noqa: E402

django.setup()

from cryptography.fernet import Fernet  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding  # noqa: E402
from django.conf import settings  # noqa: E402
from django.db import connection, transaction  # noqa: E402
from django.db.models import Count, Q  # noqa: E402
from django.utils import timezone  # noqa: E402

from iic_booking.payments import gateway_mode  # noqa: E402
from iic_booking.payments.models import (  # noqa: E402
    Payment,
    PaymentIssue,
    PaymentOrder,
    RazorpayWebhookEvent,
    SettlementLine,
)

TEXT = lambda v: str(v) if v is not None else None  # noqa: E731


def config():
    return {
        "enabled": gateway_mode.enabled(),
        "mode": gateway_mode.mode(),
        "key_mode": gateway_mode.key_mode(settings.RAZORPAY_KEY_ID),
        "live_allowed": bool(getattr(settings, "RAZORPAY_LIVE_ALLOWED", False)),
        "test_accounts_only": bool(getattr(settings, "RAZORPAY_TEST_MODE_TEST_ACCOUNTS_ONLY", True)),
        "webhook_secret_set": bool(settings.RAZORPAY_WEBHOOK_SECRET),
        "problem": gateway_mode.configuration_problem() or "none",
        "checkout_name": getattr(settings, "RAZORPAY_CHECKOUT_NAME", ""),
    }


def webhook_events():
    real = RazorpayWebhookEvent.objects.exclude(event_id__startswith="selftest_")
    return {
        "selftest_events": RazorpayWebhookEvent.objects.filter(event_id__startswith="selftest_").count(),
        "razorpay_events_by_type_and_status": list(real.values("event_type", "status").annotate(n=Count("id")).order_by("event_type")),
        "latest_razorpay_events": [
            {"event_type": e.event_type, "status": e.status, "result": e.result, "attempts": e.attempts,
             "order": e.razorpay_order_id, "payment": e.razorpay_payment_id, "amount_paise": e.amount_paise,
             "received_at": TEXT(e.received_at), "processed_at": TEXT(e.processed_at)}
            for e in real.order_by("-received_at")[:40]
        ],
    }


def _booking_filter():
    refs = [r.strip() for r in os.environ.get("BOOKING_REFS", "").split(",") if r.strip()]
    if not refs:
        return Q(mode="test")
    q = Q()
    for ref in refs:
        q |= Q(booking__virtual_booking_id=ref) | Q(external_checkout__booking__virtual_booking_id=ref)
        if ref.isdigit():
            q |= Q(booking_id=int(ref)) | Q(booking__booking_id=int(ref))
    return q


def orders():
    out = []
    for o in PaymentOrder.objects.filter(_booking_filter()).select_related("booking", "user").order_by("-created_at")[:40]:
        checkout = getattr(o, "external_checkout", None)
        booking = o.booking or getattr(checkout, "booking", None)
        payments = list(o.payments.all())
        out.append({
            "order_pk": o.pk, "receipt": o.receipt, "razorpay_order_id": o.razorpay_order_id, "purpose": o.purpose,
            "mode": o.mode, "status": o.status, "base": TEXT(o.base_amount), "fee": TEXT(o.convenience_fee),
            "fee_gst": TEXT(o.fee_gst), "total": TEXT(o.total_amount), "created_at": TEXT(o.created_at),
            "user_id": o.user_id, "user_is_test_account": bool(getattr(o.user, "is_test_account", False)),
            "booking": None if booking is None else {
                "id": booking.pk, "reference": booking.virtual_booking_id or str(booking.booking_id), "status": booking.status,
                "amount_due": TEXT(booking.amount_due), "payment_settled_at": TEXT(booking.payment_settled_at),
            },
            "checkout_state": getattr(checkout, "state", None),
            "payments": [
                {"razorpay_payment_id": p.razorpay_payment_id, "status": p.status, "amount": TEXT(p.amount), "method": p.method,
                 "verified_via": p.verified_via, "gateway_fee": TEXT(p.gateway_fee), "gateway_tax": TEXT(p.gateway_tax),
                 "settlement_lines": p.settlement_lines.count(), "created_at": TEXT(p.created_at),
                 "raw_payload_keys": sorted((p.raw_payload or {}).keys())}
                for p in payments
            ],
            "webhook_events": [
                {"event_type": e.event_type, "status": e.status, "attempts": e.attempts}
                for e in RazorpayWebhookEvent.objects.filter(razorpay_order_id=o.razorpay_order_id).order_by("received_at")
            ],
            "issues": list(PaymentIssue.objects.filter(Q(payment_order=o) | Q(razorpay_order_id=o.razorpay_order_id))
                           .values("kind", "status", "razorpay_payment_id", "amount")),
            "wallet_credits_naming_this_order": _wallet_credits(o.razorpay_order_id),
        })
    return out


def _wallet_credits(razorpay_order_id):
    from iic_booking.users.models.wallet import SubWalletTransaction

    rows = SubWalletTransaction.objects.filter(description__contains=razorpay_order_id)
    return [{"type": r.transaction_type, "amount": TEXT(r.amount), "sub_wallet_id": r.sub_wallet_id} for r in rows]


def totals():
    from iic_booking.payments.reconciliation import summary

    today = timezone.localdate()
    start = today - __import__("datetime").timedelta(days=30)
    return {"real_excluding_test": summary(start, today), "test_only": summary(start, today, mode="test")}


def issues():
    return list(PaymentIssue.objects.values("kind", "status", "mode").annotate(n=Count("id")).order_by("kind"))


def money_guards():
    real = PaymentOrder.objects.filter(mode="test", user__is_test_account=False)
    return {
        "test_orders_for_real_accounts": real.count(),
        "test_orders_total": PaymentOrder.objects.filter(mode="test").count(),
        "live_orders_total": PaymentOrder.objects.filter(mode="live").count(),
        "settlement_lines_total": SettlementLine.objects.count(),
        "captured_payments_on_test_orders": Payment.objects.filter(payment_order__mode="test").count(),
    }


def receipts():
    """The receipt each confirmed payment of the named bookings shows (only with BOOKING_REFS), exactly as the portal
    builds it, but without writing: a payment still waiting for its receipt number shows the number it would get
    (given for real the first time the receipt is opened), and stored payment details are used without asking Razorpay."""
    if not os.environ.get("BOOKING_REFS", "").strip():
        return "only with booking_refs"
    from iic_booking.payments import instrument, receipt_numbers
    from iic_booking.payments.models import ReceiptSerialCounter
    from iic_booking.payments.receipts import receipt_allowed, receipt_for

    def preview(payment):
        if payment.receipt_number:
            return payment.receipt_number
        order = payment.payment_order
        test = order.mode == "test"
        year = receipt_numbers._year(payment)
        last = ReceiptSerialCounter.objects.filter(year=year, test_mode=test).values_list("last", flat=True).first() or 0
        serial = f"TEST-{last + 1:05d}" if test else f"{last + 1:05d}"
        return f"(not given yet; next would be) IITR/{receipt_numbers.department_code(order)}/{year}/{serial}"

    receipt_numbers.ensure = preview
    instrument.ensure = lambda payment: None
    out = []
    for o in PaymentOrder.objects.filter(_booking_filter()).order_by("-created_at")[:10]:
        for p in o.payments.all():
            out.append(receipt_for(p) if receipt_allowed(p) else {"razorpay_payment_id": p.razorpay_payment_id, "no_receipt": p.status})
    return out


def section(fn):
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            return fn()
    except Exception as exc:  # noqa: BLE001 - one section failing must not lose the others
        return {"failed": type(exc).__name__}


def encrypt(payload: bytes, public_key_b64: str) -> str:
    public_key = serialization.load_pem_public_key(base64.b64decode(public_key_b64))
    key = Fernet.generate_key()
    wrapped = public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(wrapped).decode() + "." + Fernet(key).encrypt(payload).decode()


def main():
    result = {name: section(fn) for name, fn in (("config", config), ("webhook_events", webhook_events), ("orders", orders),
                                                 ("issues", issues), ("money_guards", money_guards), ("totals", totals),
                                                 ("receipts", receipts))}
    print("CIPHERTEXT " + encrypt(json.dumps(result, default=lambda v: str(v) if isinstance(v, Decimal) else str(v)).encode(),
                                  os.environ["CHECK_PUBLIC_KEY"]))


main()
