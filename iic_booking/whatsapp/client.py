"""Meta WhatsApp Business Cloud API: send an approved template message, verify webhook signatures."""

from __future__ import annotations

import hashlib
import hmac

import requests

from . import conf

TIMEOUT = (5, 15)
CODE_TIMEOUT = (3, 6)  # someone is waiting on the sign-in page, and the email has the code anyway


class WhatsAppError(Exception):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def send_template(to_e164: str, template: str, params: list[str], *, language: str | None = None,
                  session: requests.Session | None = None) -> str:
    """POST /{phone-number-id}/messages with a template and body parameters. Returns Meta's message id (wamid)."""
    url = f"{conf.GRAPH_BASE}/{conf.api_version()}/{conf.phone_number_id()}/messages"
    body = {
        "messaging_product": "whatsapp",
        "to": to_e164.lstrip("+"),
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": language or conf.template_language()},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": str(p)[:1000]} for p in params]}]
            if params else [],
        },
    }
    return _post_message(url, body, session=session, timeout=TIMEOUT)


def send_code_template(to_e164: str, template: str, code: str, *, language: str, session: requests.Session | None = None) -> str:
    """An authentication template: Meta wants the code both in the body and on the copy-code button."""
    url = f"{conf.GRAPH_BASE}/{conf.api_version()}/{conf.phone_number_id()}/messages"
    body = {
        "messaging_product": "whatsapp",
        "to": to_e164.lstrip("+"),
        "type": "template",
        "template": {
            "name": template,
            "language": {"code": language},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]},
            ],
        },
    }
    return _post_message(url, body, session=session, timeout=CODE_TIMEOUT)


def _post_message(url: str, body: dict, *, session, timeout) -> str:
    try:
        resp = (session or requests).post(url, json=body, timeout=timeout,
                                          headers={"Authorization": f"Bearer {conf.access_token()}"})
    except requests.RequestException as exc:
        raise WhatsAppError(f"network error: {type(exc).__name__}", retryable=True) from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 500 or resp.status_code == 429:
        raise WhatsAppError(f"Meta answered HTTP {resp.status_code}", retryable=True)
    if resp.status_code >= 400:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        raise WhatsAppError(f"Meta refused the message ({err.get('code', resp.status_code)}): "
                            f"{str(err.get('message') or '')[:300]}")
    messages = data.get("messages") if isinstance(data, dict) else None
    if not messages or not messages[0].get("id"):
        raise WhatsAppError("Meta's answer carried no message id")
    return str(messages[0]["id"])


def signature_valid(raw_body: bytes, header: str) -> bool:
    """X-Hub-Signature-256: sha256=<hex HMAC of the raw body with the app secret>."""
    secret = conf.app_secret()
    if not secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])
