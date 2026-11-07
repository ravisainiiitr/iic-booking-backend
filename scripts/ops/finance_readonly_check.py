"""READ-ONLY finance readiness figures. The database session is set read-only and the mailbox is opened read-only.

Run logs are public, so the figures are never printed: they are encrypted with a public key passed in, and only the holder of
the matching private key can read the printed text. Output: one line starting CIPHERTEXT, or the type name of an error.
"""

import base64
import json
import os
import statistics
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
from django.db.models import Count, Max, Min, Sum  # noqa: E402

from iic_booking.payments.models import Payment, PaymentOrder, PaymentRefund, PaymentSettlement  # noqa: E402
from iic_booking.users.models.payment import PaymentGatewayTransaction  # noqa: E402
from iic_booking.users.models.sric_wallet_recharge import SricWalletMailMessage, SricWalletRecharge, SricWalletRechargeSettings  # noqa: E402
from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction, WalletRazorpayOrder  # noqa: E402


def text(value):
    return str(value) if isinstance(value, Decimal) or value is not None else None


def duplicate_descriptions(prefix):
    """Wallet credits with exactly the same description: two credits for one reference would show here."""
    rows = (SubWalletTransaction.objects.filter(transaction_type="credit", description__startswith=prefix)
            .values("sub_wallet_id", "description", "amount").annotate(n=Count("id")).filter(n__gt=1))
    return {"groups": rows.count(), "extra_amount": text(sum(((r["n"] - 1) * r["amount"] for r in rows), Decimal("0")))}


def sbiepay():
    rows = PaymentGatewayTransaction.objects.values("gateway", "purpose", "status").annotate(n=Count("id"), total=Sum("amount"))
    return {"by_status": [{**r, "total": text(r["total"])} for r in rows],
            "newest": text(PaymentGatewayTransaction.objects.aggregate(m=Max("created_at"))["m"]),
            "credits_in_wallets": SubWalletTransaction.objects.filter(description__startswith="SBIePay recharge").count(),
            "repeat_credits": duplicate_descriptions("SBIePay recharge")}


def sric_settings():
    c = SricWalletRechargeSettings.objects.filter(pk=1).first()
    if c is None:
        return {"status": "no settings row"}
    return {"scan_enabled": c.scan_enabled, "auto_credit_enabled": c.auto_credit_enabled, "auto_credit_max_amount": text(c.auto_credit_max_amount),
            "require_internal_relay": c.require_internal_relay, "trusted_authserv_ids_count": len((c.trusted_authserv_ids or "").split()),
            "trusted_relay_ranges": (c.trusted_relay_ranges or "").split(), "gateway_marker_set": bool(c.gateway_marker_header),
            "sender_is_default": c.sender_email == "no-reply@sric.iitr.ac.in", "last_scan_at": text(c.last_scan_at)}


def sric_rows():
    credited = SricWalletRecharge.objects.filter(status="credited", is_test=False)
    amounts = sorted(credited.values_list("amount", flat=True))
    return {"by_status": list(SricWalletRecharge.objects.filter(is_test=False).values("status").annotate(n=Count("id")).order_by("status")),
            "credited_total": text(sum(amounts, Decimal("0"))), "credited_max": text(amounts[-1] if amounts else None),
            "credited_median": text(statistics.median(amounts) if amounts else None),
            "credited_p90": text(amounts[int(len(amounts) * 0.9) - 1] if amounts else None),
            "credited_without_a_person": credited.filter(credited_by__isnull=True).count(),
            "credited_fund_receipt_not_verified": credited.filter(fund_receipt_verified=False).count(),
            "credited_fund_receipt_not_verified_total": text(credited.filter(fund_receipt_verified=False).aggregate(s=Sum("amount"))["s"]),
            "emails_by_origin": list(SricWalletMailMessage.objects.filter(is_test=False, status="processed").values("authenticated").annotate(n=Count("id")))}


def wallets():
    with connection.cursor() as cur:
        cur.execute("""
            SELECT count(*), count(*) FILTER (WHERE d <> 0), coalesce(sum(abs(d)), 0), coalesce(max(abs(d)), 0)
            FROM (SELECT s.id, s.balance - coalesce(sum(CASE WHEN t.transaction_type = 'credit' THEN t.amount
                                                             WHEN t.transaction_type = 'debit' THEN -t.amount ELSE 0 END), 0) AS d
                  FROM users_subwallet s LEFT JOIN users_subwallettransaction t ON t.sub_wallet_id = s.id GROUP BY s.id) x""")
        total, differ, total_abs, largest = cur.fetchone()
    return {"sub_wallets": total, "balance_differs_from_history": differ, "sum_of_differences": text(total_abs), "largest_difference": text(largest),
            "total_balance": text(SubWallet.objects.aggregate(s=Sum("balance"))["s"]), "transactions": SubWalletTransaction.objects.count(),
            "oldest_transaction": text(SubWalletTransaction.objects.aggregate(m=Min("created_at"))["m"]),
            "repeat_booking_refunds": duplicate_descriptions("Refund for Booking")}


def razorpay():
    return {"orders": list(PaymentOrder.objects.values("purpose", "status").annotate(n=Count("id"), total=Sum("total_amount")).values_list("purpose", "status", "n")),
            "payments": list(Payment.objects.values_list("status").annotate(n=Count("id"))), "refund_rows": PaymentRefund.objects.count(),
            "settlements": PaymentSettlement.objects.count(), "key_is_live": str(getattr(settings, "RAZORPAY_KEY_ID", "")).startswith("rzp_live_"),
            "webhook_secret_set": bool(getattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")),
            "legacy_orders_left": WalletRazorpayOrder.objects.count(), "legacy_oldest": text(WalletRazorpayOrder.objects.aggregate(m=Min("created_at"))["m"])}


def legacy_switches():
    return {"sric_legacy_transfer_endpoint": bool(getattr(settings, "SRIC_LEGACY_TRANSFER_ENDPOINT_ENABLED", False)),
            "sric_api_key_set": bool(getattr(settings, "SRIC_API_KEY", ""))}


def recent_email_hops(limit=20):
    """How the Received lines of recent SRIC emails look, to see what a stricter origin rule would do. Counts only."""
    import email
    import re

    from iic_booking.users import sric_wallet_recharge as svc
    from iic_booking.users.imap_fetch import connect_imap
    from iic_booking.users.sric_wallet_mail_auth import _hop_ips

    imap, config = svc._imap_config(), SricWalletRechargeSettings.objects.filter(pk=1).first()
    if imap is None or config is None:
        return {"status": "mailbox or settings not configured"}
    folder = (getattr(settings, "IMAP_MAILBOX", "") or "INBOX").strip() or "INBOX"
    conn = connect_imap(imap["host"], imap["port"], imap["use_ssl"], imap["email_address"], imap["password"], timeout=imap["timeout"])
    out = {"emails": 0, "hops": 0, "hops_from_with_no_address": 0, "emails_affected": 0, "hops_with_no_from": 0}
    try:
        if conn.select(folder, readonly=True)[0] != "OK":
            return {"status": "folder not opened"}
        for uid in svc._search_uids(conn, config.sender_email, svc._cutoff_date())[-limit:]:
            raw = svc._fetch(conn, uid)
            if raw is None:
                continue
            hops = email.message_from_bytes(raw).get_all("Received") or []
            bad = [h for h in hops if re.match(r"\s*from\b", str(h), flags=re.I) and not _hop_ips(h)]
            out["emails"] += 1
            out["hops"] += len(hops)
            out["hops_from_with_no_address"] += len(bad)
            out["emails_affected"] += int(bool(bad))
            out["hops_with_no_from"] += sum(1 for h in hops if not re.match(r"\s*from\b", str(h), flags=re.I))
    finally:
        try:
            conn.logout()
        except Exception:  # noqa: BLE001
            pass
    return out


def section(fn):
    try:
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SET TRANSACTION READ ONLY")
            return fn()
    except Exception as exc:  # noqa: BLE001 - one section failing must not lose the others; the type name says enough
        return {"failed": type(exc).__name__}


def encrypt(payload: bytes, public_key_b64: str) -> str:
    public_key = serialization.load_pem_public_key(base64.b64decode(public_key_b64))
    key = Fernet.generate_key()
    wrapped = public_key.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None))
    return base64.b64encode(wrapped).decode() + "." + Fernet(key).encrypt(payload).decode()


def main():
    result = {name: section(fn) for name, fn in (("sbiepay", sbiepay), ("sric_settings", sric_settings), ("sric_rows", sric_rows), ("wallets", wallets),
                                                 ("razorpay", razorpay), ("legacy_switches", legacy_switches))}
    try:
        result["recent_email_hops"] = recent_email_hops()
    except Exception as exc:  # noqa: BLE001
        result["recent_email_hops"] = {"failed": type(exc).__name__}
    print("CIPHERTEXT " + encrypt(json.dumps(result, default=str).encode(), os.environ["CHECK_PUBLIC_KEY"]))


main()
