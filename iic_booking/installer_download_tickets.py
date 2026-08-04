"""Short-lived signed tickets for native browser installer downloads.

Avoids buffering large EXE/ZIP files in the SPA via fetch(). The portal issues
a HMAC ticket after auth; the browser then navigates to a ticket URL and the
file streams directly (OS download progress, no double RAM copy).
"""

from __future__ import annotations

from typing import Any

from django.core import signing
from django.http import HttpRequest

_SALT = "iic-installer-download-v1"
_MAX_AGE_SECONDS = 10 * 60  # 10 minutes


def issue_ticket(
    *,
    product: str,
    release_id: str,
    offline: bool,
    user_id: int | str | None = None,
) -> str:
    payload = {
        "p": product,
        "r": str(release_id),
        "o": 1 if offline else 0,
        "u": str(user_id) if user_id is not None else "",
    }
    return signing.dumps(payload, salt=_SALT, compress=True)


def parse_ticket(token: str, *, max_age: int = _MAX_AGE_SECONDS) -> dict[str, Any]:
    data = signing.loads(token, salt=_SALT, max_age=max_age)
    if not isinstance(data, dict) or not data.get("r") or not data.get("p"):
        raise signing.BadSignature("invalid ticket payload")
    return data


def ticket_download_path(product: str, token: str) -> str:
    if product == "dsa":
        return f"/api/v1/sync/installer/releases/download/ticket/{token}/"
    if product == "eq_wizard":
        return f"/api/v1/deployment/wizard/download/{token}/"
    return f"/api/v1/analysis/installer/releases/download/ticket/{token}/"


def absolute_ticket_url(request: HttpRequest, product: str, token: str) -> str:
    path = ticket_download_path(product, token)
    return request.build_absolute_uri(path)
