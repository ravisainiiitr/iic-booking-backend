"""Waking a callee's apps for an incoming call: Android through Firebase Cloud Messaging (HTTP v1, high-priority data
message) and iOS through an APNs VoIP push (PushKit -> CallKit). Both are off until their credentials are configured
(never in the repository). Only incoming-call events are sent this way, never ordinary notifications.
Tokens, keys and access tokens are never logged.
"""

from __future__ import annotations

import base64
import json
import logging
import time
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from django.conf import settings
from django.core.cache import cache

from iic_booking.communication.web_push import b64url

logger = logging.getLogger(__name__)

FCM_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_GONE = {"UNREGISTERED", "NOT_FOUND", "INVALID_ARGUMENT"}


def _jwt(header: dict, claims: dict, sign) -> str:
    signing_input = f"{b64url(json.dumps(header, separators=(',', ':')).encode())}.{b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    return f"{signing_input}.{b64url(sign(signing_input.encode()))}"


# --- Android: FCM HTTP v1 ---------------------------------------------------------------------------------------------

def _fcm_account() -> dict | None:
    encoded = getattr(settings, "CALLS_FCM_SERVICE_ACCOUNT_B64", "")
    if encoded:
        try:
            return json.loads(base64.b64decode(encoded))
        except ValueError as exc:
            logger.warning("CALLS_FCM_SERVICE_ACCOUNT_B64 cannot be read: %s", type(exc).__name__)
            return None
    path = getattr(settings, "CALLS_FCM_SERVICE_ACCOUNT_FILE", "")
    if not path or not Path(path).is_file():
        return None
    return json.loads(Path(path).read_text(encoding="utf-8"))


def fcm_configured() -> bool:
    return _fcm_account() is not None


def _fcm_access_token(account: dict) -> str:
    cached = cache.get("calls:fcm:access")
    if cached:
        return cached
    key = serialization.load_pem_private_key(account["private_key"].encode(), password=None)
    now = int(time.time())
    assertion = _jwt({"alg": "RS256", "typ": "JWT"},
                     {"iss": account["client_email"], "scope": FCM_SCOPE, "aud": account["token_uri"], "iat": now,
                      "exp": now + 3600},
                     lambda data: key.sign(data, padding.PKCS1v15(), hashes.SHA256()))
    resp = requests.post(account["token_uri"], data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                     "assertion": assertion}, timeout=10)
    resp.raise_for_status()
    token = resp.json()["access_token"]
    cache.set("calls:fcm:access", token, 3000)
    return token


def send_fcm(token: str, data: dict[str, str], ttl_seconds: int) -> str:
    """'sent', 'gone' (token no longer valid) or 'failed'."""
    account = _fcm_account()
    if account is None:
        return "failed"
    body = {"message": {"token": token, "data": {k: str(v) for k, v in data.items()},
                        "android": {"priority": "HIGH", "ttl": f"{int(ttl_seconds)}s"}}}
    try:
        resp = requests.post(f"https://fcm.googleapis.com/v1/projects/{account['project_id']}/messages:send",
                             headers={"Authorization": f"Bearer {_fcm_access_token(account)}"}, json=body, timeout=10)
    except Exception as exc:  # noqa: BLE001
        logger.warning("FCM call push failed: %s", type(exc).__name__)
        return "failed"
    if resp.status_code == 200:
        return "sent"
    status = ((resp.json() if resp.content else {}).get("error") or {}).get("status", "") if resp.headers.get(
        "content-type", "").startswith("application/json") else ""
    logger.warning("FCM call push refused: http=%s status=%s", resp.status_code, status)
    return "gone" if resp.status_code in (404, 410) or status in _GONE else "failed"


# --- iOS: APNs VoIP (token-based auth, HTTP/2) ------------------------------------------------------------------------

def apns_configured() -> bool:
    path = getattr(settings, "CALLS_APNS_KEY_FILE", "")
    return bool(path and Path(path).is_file() and getattr(settings, "CALLS_APNS_KEY_ID", "")
                and getattr(settings, "CALLS_APNS_TEAM_ID", ""))


def _apns_jwt() -> str:
    cached = cache.get("calls:apns:jwt")
    if cached:
        return cached
    key = serialization.load_pem_private_key(Path(settings.CALLS_APNS_KEY_FILE).read_bytes(), password=None)

    def sign(data: bytes) -> bytes:
        r, s = decode_dss_signature(key.sign(data, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")

    token = _jwt({"alg": "ES256", "kid": settings.CALLS_APNS_KEY_ID},
                 {"iss": settings.CALLS_APNS_TEAM_ID, "iat": int(time.time())}, sign)
    cache.set("calls:apns:jwt", token, 2400)  # Apple accepts a token for up to an hour
    return token


def send_apns_voip(token: str, data: dict[str, str], ttl_seconds: int) -> str:
    if not apns_configured():
        return "failed"
    try:
        import httpx
    except ImportError:
        logger.warning("APNs VoIP push unavailable: httpx[http2] is not installed")
        return "failed"
    host = "api.sandbox.push.apple.com" if getattr(settings, "CALLS_APNS_USE_SANDBOX", True) else "api.push.apple.com"
    headers = {
        "authorization": f"bearer {_apns_jwt()}",
        "apns-topic": f"{settings.CALLS_APNS_BUNDLE_ID}.voip",
        "apns-push-type": "voip",
        "apns-priority": "10",
        "apns-expiration": str(int(time.time()) + int(ttl_seconds)),
    }
    try:
        with httpx.Client(http2=True, timeout=10) as client:
            resp = client.post(f"https://{host}/3/device/{token}", headers=headers, json=data)
    except Exception as exc:  # noqa: BLE001
        logger.warning("APNs call push failed: %s", type(exc).__name__)
        return "failed"
    if resp.status_code == 200:
        return "sent"
    reason = ""
    try:
        reason = resp.json().get("reason", "")
    except ValueError:
        pass
    logger.warning("APNs call push refused: http=%s reason=%s", resp.status_code, reason)
    return "gone" if resp.status_code == 410 or reason in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic") else "failed"


def ring_devices(session, data: dict[str, str]) -> int:
    """Push the incoming call to every active install of the callee; revokes tokens the services reject."""
    from django.utils import timezone

    from .models import CallDevice, DevicePlatform

    sent = 0
    ttl = int(getattr(settings, "CALLS_RING_SECONDS", 45))
    for device in CallDevice.objects.filter(user=session.callee, revoked_at__isnull=True):
        sender = send_fcm if device.platform == DevicePlatform.ANDROID else send_apns_voip
        result = sender(device.token, data, ttl)
        if result == "sent":
            sent += 1
        elif result == "gone":
            device.revoked_at = timezone.now()
            device.save(update_fields=["revoked_at"])
    return sent


def stop_ringing_devices(session) -> int:
    """The call stopped ringing (answered, declined, cancelled, timed out): tell the callee's Android installs to stop.
    Android only: iOS must report every VoIP push as a new call, so its CallKit screen is ended by the app itself."""
    from .models import CallDevice, DevicePlatform

    if not fcm_configured():
        return 0
    from iic_booking.users.display import get_user_display_name

    # How it ended and who rang let the phone show "Missed call". Apps that do not know these fields ignore them.
    data = {"type": "call_ended", "call_id": str(session.id), "status": str(session.status),
            "caller_name": get_user_display_name(session.caller)}
    sent = 0
    for device in CallDevice.objects.filter(user=session.callee, platform=DevicePlatform.ANDROID, revoked_at__isnull=True):
        sent += send_fcm(device.token, data, 60) == "sent"
    return sent
