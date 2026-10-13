"""Browser push notifications (Web Push with VAPID, RFC 8030 / 8291 / 8292).

Pushes reach a user's browser while the portal is closed. The VAPID key pair is derived from SECRET_KEY, so it
survives deploys without extra configuration; rotating SECRET_KEY makes browsers subscribe again on their next
visit (the portal compares the stored key with the server's).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from functools import lru_cache
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings

logger = logging.getLogger(__name__)

_P256_ORDER = int("FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16)
RECORD_SIZE = 4096
TTL_SECONDS = 24 * 3600
MAX_FAILURES = 5


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    value = (value or "").strip()
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


@lru_cache(maxsize=1)
def _vapid_private_key() -> ec.EllipticCurvePrivateKey:
    seed = _hkdf(b"iic-web-push-vapid", settings.SECRET_KEY.encode("utf-8"), b"vapid-p256", 48)
    scalar = int.from_bytes(seed, "big") % (_P256_ORDER - 1) + 1
    return ec.derive_private_key(scalar, ec.SECP256R1())


def _public_bytes(key) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def vapid_public_key() -> str:
    """The applicationServerKey browsers subscribe with (base64url, uncompressed P-256 point)."""
    return b64url(_public_bytes(_vapid_private_key().public_key()))


def _vapid_subject() -> str:
    email = (getattr(settings, "DEFAULT_FROM_EMAIL", "") or "").split("<")[-1].strip(" >")
    if "@" in email:
        return f"mailto:{email}"
    return (getattr(settings, "FRONTEND_URL", "") or "https://equip.iitr.ac.in").rstrip("/")


def vapid_authorization(endpoint: str, now: float | None = None) -> str:
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((now or time.time()) + 12 * 3600),
        "sub": _vapid_subject(),
    }
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode("ascii")
    der = _vapid_private_key().sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    return f"vapid t={header}.{body}.{signature}, k={vapid_public_key()}"


def encrypt_payload(payload: bytes, p256dh: str, auth: str, *, salt: bytes | None = None, sender_key=None) -> bytes:
    """aes128gcm content coding of ``payload`` for one subscription (RFC 8291 section 3.4)."""
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    if len(ua_public) != 65 or len(auth_secret) < 16:
        raise ValueError("Invalid push subscription keys.")
    sender = sender_key or ec.generate_private_key(ec.SECP256R1())
    as_public = _public_bytes(sender.public_key())
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = _hkdf(auth_secret, shared, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = salt or os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    if len(payload) + 1 + 16 > RECORD_SIZE:
        raise ValueError("Push payload is too large.")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    header = salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public
    return header + ciphertext


def payload_for(log) -> bytes:
    meta = log.metadata or {}
    data = {
        "title": (log.subject or "IIC Booking")[:120],
        "body": (log.message or "")[:300],
        "link": str(meta.get("link") or "/notifications"),
        "tag": f"notification-{log.pk}",
        "id": log.pk,
    }
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def send_to_subscription(subscription, payload: bytes) -> bool:
    """POST one encrypted message; drops subscriptions the push service no longer accepts."""
    import requests
    from django.utils import timezone

    body = encrypt_payload(payload, subscription.p256dh, subscription.auth)
    try:
        response = requests.post(
            subscription.endpoint,
            data=body,
            headers={
                "Authorization": vapid_authorization(subscription.endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(TTL_SECONDS),
                "Urgency": "normal",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Web push to subscription %s failed: %s", subscription.pk, exc)
        status_code = None
    else:
        status_code = response.status_code
    if status_code in (404, 410):
        subscription.delete()
        return False
    if status_code is not None and 200 <= status_code < 300:
        subscription.failure_count = 0
        subscription.last_success_at = timezone.now()
        subscription.save(update_fields=["failure_count", "last_success_at"])
        return True
    subscription.failure_count += 1
    if subscription.failure_count >= MAX_FAILURES:
        subscription.delete()
    else:
        subscription.save(update_fields=["failure_count"])
    logger.info("Web push to subscription %s answered %s", subscription.pk, status_code)
    return False


def deliver_for_log(log) -> int:
    """Push an in-app notification to every browser of its recipient; returns how many accepted it."""
    from . import notification_preferences as prefs
    from .models import WebPushSubscription

    user = log.recipient
    if user is None:
        return 0
    category = (log.metadata or {}).get("notification_category") or prefs.OTHER
    if not prefs.push_allowed_now(user, category):
        return 0
    payload = payload_for(log)
    sent = 0
    for subscription in WebPushSubscription.objects.filter(user=user):
        try:
            sent += int(send_to_subscription(subscription, payload))
        except Exception:
            logger.exception("Web push failed for subscription %s", subscription.pk)
    return sent
