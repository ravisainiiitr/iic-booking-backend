"""FCM (legacy HTTP) delivery helper — no-op unless FCM_SERVER_KEY is configured."""

from __future__ import annotations

import json
import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger(__name__)


def send_fcm_to_token(
    *,
    token: str,
    title: str,
    body: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Send a notification via FCM legacy HTTP API.

    Returns {"ok": True/False, ...}. When FCM_SERVER_KEY is unset, returns skipped.
    """
    server_key = (getattr(settings, "FCM_SERVER_KEY", None) or "").strip()
    if not server_key:
        return {"ok": False, "skipped": True, "reason": "fcm_not_configured"}

    payload = {
        "to": token,
        "notification": {"title": title or "", "body": body or ""},
        "data": {str(k): str(v) for k, v in (data or {}).items()},
        "priority": "high",
    }
    try:
        resp = requests.post(
            "https://fcm.googleapis.com/fcm/send",
            headers={
                "Authorization": f"key={server_key}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload),
            timeout=10,
        )
        ok = 200 <= resp.status_code < 300
        detail: Any
        try:
            detail = resp.json()
        except Exception:
            detail = {"raw": resp.text[:500]}
        if not ok:
            logger.warning("FCM send failed status=%s detail=%s", resp.status_code, detail)
        return {"ok": ok, "status_code": resp.status_code, "detail": detail}
    except Exception as exc:  # noqa: BLE001
        logger.warning("FCM send exception: %s", exc)
        return {"ok": False, "error": str(exc)}


def deliver_to_user_devices(*, user, title: str, message: str, metadata: dict | None = None) -> dict:
    from iic_booking.communication.models import PushDevice

    devices = PushDevice.objects.filter(user=user, is_active=True)
    results = []
    for device in devices:
        results.append(
            {
                "device_id": device.id,
                "platform": device.platform,
                **send_fcm_to_token(
                    token=device.token,
                    title=title,
                    body=message,
                    data={
                        "link": (metadata or {}).get("link") or "",
                        "notification_type": (metadata or {}).get("notification_type") or "info",
                        "event": (metadata or {}).get("event") or "",
                    },
                ),
            }
        )
    return {"device_count": devices.count(), "results": results}
