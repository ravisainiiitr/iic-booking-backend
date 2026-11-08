"""How a payment was made, for the receipt: UPI / card / netbanking / wallet details from Razorpay's payment entity.

Only receipt-safe fields are kept (in ``Payment.raw_payload["instrument"]``): the bank's references (UPI RRN and
transaction ID, netbanking transaction no., card authorisation code), the payer's bank, the card network, type and last
four digits, the wallet, and the UPI ID with its name part masked. Never a full card number, expiry, email or phone,
and nothing about the institute's own bank account.
"""

from __future__ import annotations

import logging

from .models import Payment

logger = logging.getLogger(__name__)

KEY = "instrument"

BANKS = {
    "SBIN": "State Bank of India", "HDFC": "HDFC Bank", "ICIC": "ICICI Bank", "UTIB": "Axis Bank",
    "KKBK": "Kotak Mahindra Bank", "PUNB": "Punjab National Bank", "PUNB_R": "Punjab National Bank", "BARB": "Bank of Baroda",
    "BARB_R": "Bank of Baroda", "CNRB": "Canara Bank", "UBIN": "Union Bank of India", "IDIB": "Indian Bank",
    "IOBA": "Indian Overseas Bank", "BKID": "Bank of India", "MAHB": "Bank of Maharashtra", "CBIN": "Central Bank of India",
    "UCBA": "UCO Bank", "PSIB": "Punjab & Sind Bank", "YESB": "Yes Bank", "INDB": "IndusInd Bank", "IDFB": "IDFC FIRST Bank",
    "FDRL": "Federal Bank", "RATN": "RBL Bank", "KARB": "Karnataka Bank", "SIBL": "South Indian Bank", "AUBL": "AU Small Finance Bank",
    "CIUB": "City Union Bank", "DBSS": "DBS Bank India", "SCBL": "Standard Chartered Bank", "HSBC": "HSBC", "CITI": "Citibank",
    "IBKL": "IDBI Bank", "JAKA": "Jammu & Kashmir Bank", "TMBL": "Tamilnad Mercantile Bank", "KVBL": "Karur Vysya Bank",
    "DCBL": "DCB Bank", "ESFB": "Equitas Small Finance Bank", "USFB": "Ujjivan Small Finance Bank", "BDBL": "Bandhan Bank",
    "CSBK": "CSB Bank", "DLXB": "Dhanlaxmi Bank", "NTBL": "Nainital Bank", "LAVB": "Lakshmi Vilas Bank",
}
# UPI handle -> the bank or app behind it (the payer's own bank is not reported for UPI).
UPI_HANDLES = {
    "okhdfcbank": "HDFC Bank (Google Pay)", "okicici": "ICICI Bank (Google Pay)", "oksbi": "State Bank of India (Google Pay)",
    "okaxis": "Axis Bank (Google Pay)", "ybl": "Yes Bank (PhonePe)", "ibl": "ICICI Bank (PhonePe)", "axl": "Axis Bank (PhonePe)",
    "paytm": "Paytm Payments Bank", "ptyes": "Yes Bank (Paytm)", "ptaxis": "Axis Bank (Paytm)", "pthdfc": "HDFC Bank (Paytm)",
    "ptsbi": "State Bank of India (Paytm)", "upi": "BHIM UPI", "sbi": "State Bank of India", "hdfcbank": "HDFC Bank",
    "icici": "ICICI Bank", "axisbank": "Axis Bank", "kotak": "Kotak Mahindra Bank", "apl": "Axis Bank (Amazon Pay)",
    "yapl": "Yes Bank (Amazon Pay)", "jupiteraxis": "Axis Bank (Jupiter)", "fbl": "Federal Bank", "pnb": "Punjab National Bank",
    "barodampay": "Bank of Baroda", "cnrb": "Canara Bank", "unionbank": "Union Bank of India", "indus": "IndusInd Bank",
    "idfcbank": "IDFC FIRST Bank", "rbl": "RBL Bank", "aubank": "AU Small Finance Bank", "razorpay": "Razorpay test UPI",
    "success": "Razorpay test UPI",
}
WALLETS = {"paytm": "Paytm", "phonepe": "PhonePe", "amazonpay": "Amazon Pay", "mobikwik": "MobiKwik", "freecharge": "Freecharge",
           "airtelmoney": "Airtel Payments Bank", "jiomoney": "JioMoney", "olamoney": "Ola Money", "payzapp": "PayZapp"}
NETWORKS = {"Visa": "Visa", "MasterCard": "Mastercard", "RuPay": "RuPay", "American Express": "American Express",
            "Diners Club": "Diners Club", "Maestro": "Maestro", "JCB": "JCB", "Bajaj Finserv": "Bajaj Finserv"}


def _text(value, limit: int = 64) -> str:
    return str(value).strip()[:limit] if value not in (None, "", False) else ""


def mask_vpa(vpa: str) -> str:
    name, _, handle = (vpa or "").partition("@")
    if not handle:
        return ""
    return f"{name[:2]}{'*' * max(3, len(name) - 2)}@{handle}" if name else f"***@{handle}"


def extract(entity: dict | None) -> dict:
    """Receipt-safe details of a Razorpay payment entity ({} when there is nothing usable)."""
    if not isinstance(entity, dict):
        return {}
    acquirer = entity.get("acquirer_data") if isinstance(entity.get("acquirer_data"), dict) else {}
    card = entity.get("card") if isinstance(entity.get("card"), dict) else {}
    upi = entity.get("upi") if isinstance(entity.get("upi"), dict) else {}
    vpa = _text(entity.get("vpa") or upi.get("vpa"), 128)
    out = {
        "method": _text(entity.get("method"), 24),
        "bank": _text(entity.get("bank"), 16),
        "wallet": _text(entity.get("wallet"), 32),
        "vpa_masked": mask_vpa(vpa),
        "upi_handle": vpa.partition("@")[2].lower()[:64] if "@" in vpa else "",
        "rrn": _text(acquirer.get("rrn")),
        "upi_transaction_id": _text(acquirer.get("upi_transaction_id")),
        "bank_transaction_id": _text(acquirer.get("bank_transaction_id") or entity.get("bank_transaction_id")),
        "auth_code": _text(acquirer.get("auth_code"), 16),
        "card_network": _text(card.get("network"), 32),
        "card_type": _text(card.get("type"), 16),
        "card_issuer": _text(card.get("issuer"), 16),
        "card_last4": _text(card.get("last4"), 4) if str(card.get("last4") or "").isdigit() else "",
        "international": bool(entity.get("international") or card.get("international")),
        "emi": bool(card.get("emi")) if card else False,
    }
    return {k: v for k, v in out.items() if v not in ("", False)}


def record(razorpay_payment_id: str, entity: dict | None) -> None:
    """Keep the receipt-safe details on the payment. Never raises; an existing record is not overwritten."""
    details = extract(entity)
    if not details:
        return
    try:
        payment = Payment.objects.filter(razorpay_payment_id=razorpay_payment_id).first()
        if payment is None or (payment.raw_payload or {}).get(KEY):
            return
        payment.raw_payload = {**(payment.raw_payload or {}), KEY: details}
        payment.save(update_fields=["raw_payload", "updated_at"])
    except Exception:
        logger.exception("could not store payment details for %s", razorpay_payment_id)


def ensure(payment: Payment) -> dict:
    """The payment's details; for payments recorded before 8 Nov 2026, fetched once from Razorpay (best effort)."""
    from django.conf import settings
    from django.core.cache import cache

    stored = (payment.raw_payload or {}).get(KEY)
    if isinstance(stored, dict) and stored:
        return stored
    backoff = f"payments:instrument:miss:{payment.razorpay_payment_id}"
    if not getattr(settings, "RECEIPT_FETCH_PAYMENT_DETAILS", True) or cache.get(backoff):
        return {}
    try:
        from .razorpay_service import get_razorpay_client

        entity = get_razorpay_client().payment.fetch(payment.razorpay_payment_id)
    except Exception:
        logger.info("payment details unavailable from Razorpay for %s", payment.razorpay_payment_id)
        cache.set(backoff, 1, 3600)  # try again in an hour, not on every receipt view
        return {}
    if not isinstance(entity, dict) or entity.get("order_id") != payment.payment_order.razorpay_order_id:
        return {}
    record(payment.razorpay_payment_id, entity)
    payment.refresh_from_db(fields=["raw_payload"])
    return (payment.raw_payload or {}).get(KEY) or {}


def _bank(code: str) -> str:
    return BANKS.get(code.upper(), code.upper()) if code else ""


def describe(payment: Payment) -> dict:
    """{"mode": "UPI", "details": [[label, value], ...]} for the receipt, best first. Falls back to what the payment
    row holds (method, bank reference) when Razorpay's details are unavailable."""
    d = ensure(payment)
    method = (d.get("method") or payment.method or "").lower()
    rows: list[list[str]] = []

    def add(label, value):
        if value:
            rows.append([label, value])

    if method == "upi":
        mode = "UPI"
        add("UPI reference no. (RRN)", d.get("rrn") or payment.gateway_reference)
        if d.get("upi_transaction_id") and d.get("upi_transaction_id") != d.get("rrn"):
            add("UPI transaction ID", d["upi_transaction_id"])
        add("Payer UPI ID", d.get("vpa_masked"))
        add("Payer bank / app", UPI_HANDLES.get(d.get("upi_handle", ""), "") or _bank(d.get("bank", "")))
    elif method == "card":
        kind = {"credit": "Credit card", "debit": "Debit card", "prepaid": "Prepaid card"}.get(d.get("card_type", ""), "Card")
        network = NETWORKS.get(d.get("card_network", ""), d.get("card_network", ""))
        mode = f"{kind}{f' ({network})' if network else ''}{', EMI' if d.get('emi') else ''}"
        add("Card number", f"XXXX XXXX XXXX {d['card_last4']}" if d.get("card_last4") else "")
        add("Issuing bank", _bank(d.get("card_issuer", "")))
        add("Authorisation code", d.get("auth_code"))
        add("Bank reference no. (RRN)", d.get("rrn") or payment.gateway_reference)
        if d.get("international"):
            add("Card origin", "International")
    elif method == "netbanking":
        mode = "Net banking"
        add("Bank", _bank(d.get("bank", "")))
        add("Bank transaction reference", d.get("bank_transaction_id") or payment.gateway_reference)
    elif method == "wallet":
        mode = "Wallet"
        add("Wallet", WALLETS.get(d.get("wallet", "").lower(), d.get("wallet", "").title()))
        add("Wallet transaction reference", d.get("bank_transaction_id") or payment.gateway_reference)
    elif method in ("emi", "cardless_emi", "paylater"):
        mode = {"emi": "EMI", "cardless_emi": "Cardless EMI", "paylater": "Pay later"}[method]
        add("Provider", _bank(d.get("bank", "")) or d.get("wallet", "").title())
        add("Bank reference", d.get("rrn") or d.get("bank_transaction_id") or payment.gateway_reference)
    else:
        mode = method.upper() or "Online"
        add("Bank reference", d.get("rrn") or d.get("bank_transaction_id") or payment.gateway_reference)
    return {"mode": mode, "details": rows}
