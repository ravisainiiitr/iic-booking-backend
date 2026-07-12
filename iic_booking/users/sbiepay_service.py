"""
SBIePay payment gateway helpers (aggregator-hosted redirect model).

Configure via settings after merchant onboarding (MID + encryption key from SBI kit).
Pipe format and encryption follow SBIePay Merchant Integration Guide v2.3.x.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from decimal import Decimal
from typing import Any, Optional
from urllib.parse import urlencode

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _sbiepay_configured() -> bool:
    return bool(
        getattr(settings, "SBIEPAY_MERCHANT_ID", "")
        and getattr(settings, "SBIEPAY_ENCRYPTION_KEY", "")
    )


def _encryption_key_bytes() -> bytes:
    key = (getattr(settings, "SBIEPAY_ENCRYPTION_KEY", "") or "").strip()
    # SBI kit keys are often 16/24/32 chars — pad or hash to 16 bytes for AES-128
    raw = key.encode("utf-8")
    if len(raw) in (16, 24, 32):
        return raw
    return hashlib.md5(raw).digest()


def encrypt_pipe_string(plain: str) -> str:
    """AES-128-CBC encrypt; returns base64 ciphertext (SBIePay EncryptTrans format)."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key = _encryption_key_bytes()[:16]
    iv = key  # SBI legacy kits often use key as IV
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain.encode("utf-8")) + padder.finalize()
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(encrypted).decode("ascii")


def decrypt_pipe_string(cipher_b64: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding

    key = _encryption_key_bytes()[:16]
    iv = key
    data = base64.b64decode(cipher_b64)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(data) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8", errors="replace")


def generate_merchant_order_ref(prefix: str = "IIC") -> str:
    ts = timezone.now().strftime("%Y%m%d%H%M%S")
    suffix = secrets.token_hex(3).upper()
    return f"{prefix}-{ts}-{suffix}"[:64]


def build_transaction_pipe(
    *,
    amount_inr: Decimal,
    merchant_order_ref: str,
    success_url: str,
    failure_url: str,
) -> str:
    """
    Build decrypted EncryptTrans pipe string.
    Field order per SBIePay aggregator integration — confirm against your kit if UAT fails.
    """
    mid = getattr(settings, "SBIEPAY_MERCHANT_ID", "")
    amount_str = str(int((amount_inr * 100).quantize(Decimal("1"))))  # paise
    domain = getattr(settings, "SBIEPAY_DOMAIN", "DOM")
    country = getattr(settings, "SBIEPAY_COUNTRY", "IN")
    currency = getattr(settings, "SBIEPAY_CURRENCY", "INR")
    paymode = getattr(settings, "SBIEPAY_PAYMODE", "ONLINE")
    # merchantId|DOM|IN|INR|amount|Other|successUrl|failUrl|orderRef|...
    return "|".join(
        [
            mid,
            domain,
            country,
            currency,
            amount_str,
            "Other",
            success_url,
            failure_url,
            merchant_order_ref,
            paymode,
            "ONLINE",
        ]
    )


def build_initiate_payload(
    *,
    amount_inr: Decimal,
    merchant_order_ref: str,
    success_url: Optional[str] = None,
    failure_url: Optional[str] = None,
) -> dict[str, Any]:
    if not _sbiepay_configured():
        raise ValueError("SBIePay is not configured. Contact administrator.")

    success_url = success_url or getattr(settings, "SBIEPAY_SUCCESS_URL", "")
    failure_url = failure_url or getattr(settings, "SBIEPAY_FAILURE_URL", "")
    if not success_url or not failure_url:
        raise ValueError("SBIePay success/failure URLs are not configured.")

    plain = build_transaction_pipe(
        amount_inr=amount_inr,
        merchant_order_ref=merchant_order_ref,
        success_url=success_url,
        failure_url=failure_url,
    )
    encrypt_trans = encrypt_pipe_string(plain)
    gateway_url = getattr(
        settings,
        "SBIEPAY_GATEWAY_URL",
        "https://test.sbiepay.sbi/secure/AggregatorHostedListener",
    )
    return {
        "gateway_url": gateway_url,
        "merchant_id": getattr(settings, "SBIEPAY_MERCHANT_ID", ""),
        "encrypt_trans": encrypt_trans,
        "merchant_order_ref": merchant_order_ref,
        "form_method": "POST",
        "form_fields": {
            "EncryptTrans": encrypt_trans,
            "merchIdVal": getattr(settings, "SBIEPAY_MERCHANT_ID", ""),
        },
    }


def parse_gateway_response(encrypted_or_plain: str) -> dict[str, str]:
    """Parse SBI return / push response into key-value pairs."""
    text = encrypted_or_plain.strip()
    if not text:
        return {}
    if "=" not in text and len(text) > 40 and not text.startswith("{"):
        try:
            text = decrypt_pipe_string(text)
        except Exception:
            logger.exception("SBIePay decrypt failed")
            return {"raw": encrypted_or_plain}
    if "|" in text:
        parts = text.split("|")
        keys = [
            "merchant_id",
            "status",
            "amount",
            "currency",
            "merchant_order_ref",
            "gateway_ref",
            "bank_ref",
            "other",
        ]
        return {keys[i] if i < len(keys) else f"f{i}": parts[i] for i in range(len(parts))}
    return {"raw": text}
