"""Meta's WhatsApp Business Management API for templates: list, create and edit. It never sends a message.

The access token travels only in a header, and every error from Meta is cleaned before it is kept or printed.
"""

from __future__ import annotations

import re

import requests

from . import conf

TIMEOUT = (5, 30)
MAX_PAGES = 50
PAGE_SIZE = 100
FIELDS = "id,name,language,status,category,rejected_reason,components"
RATE_LIMIT_CODES = {4, 17, 32, 613, 80007, 80008}
DUPLICATE_SUBCODE = 2388024
_LONG_NUMBER = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_TOKEN_LIKE = re.compile(r"\bEA[A-Za-z0-9]{10,}\b")
_URL = re.compile(r"https?://\S+")


class TemplateApiError(Exception):
    def __init__(self, kind: str, message: str, *, http_status: int | None = None, code=None, subcode=None):
        super().__init__(message)
        self.kind, self.http_status, self.code, self.subcode = kind, http_status, code, subcode


def clean(text, *, secrets=()) -> str:
    """No token, id, number or address from Meta's text reaches a log or the audit table."""
    value = str(text or "")
    for secret in sorted((s for s in (conf.access_token(), conf.waba_id(), conf.app_secret(), *secrets) if s), key=len, reverse=True):
        value = value.replace(secret, "[hidden]")
    value = _URL.sub("[url]", value)
    value = _TOKEN_LIKE.sub("[token]", value)
    return _LONG_NUMBER.sub("[number]", value)[:500]


class TemplateClient:
    def __init__(self, session=None):
        self.session = session or requests

    def _url(self, path: str) -> str:
        return f"{conf.GRAPH_BASE}/{conf.api_version()}/{path}"

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {conf.access_token()}"}

    def _read(self, response) -> dict:
        try:
            data = response.json()
        except ValueError:
            data = {}
        status = response.status_code
        if status < 400 and not (isinstance(data, dict) and "error" in data):
            return data if isinstance(data, dict) else {}
        error = data.get("error") if isinstance(data, dict) and isinstance(data.get("error"), dict) else {}
        code, subcode = error.get("code"), error.get("error_subcode")
        if status == 429 or code in RATE_LIMIT_CODES:
            kind = "rate_limit"
        elif subcode == DUPLICATE_SUBCODE:
            kind = "duplicate"
        elif code == 190:
            kind = "token_invalid"
        elif status >= 500:
            kind = "server_error"
        elif code in (3, 10, 200) or status == 403:
            kind = "permission"
        else:
            kind = "refused"
        details = error.get("error_user_msg") or error.get("message") or ""
        raise TemplateApiError(kind, clean(details), http_status=status, code=code, subcode=subcode)

    def _get(self, path: str, params: dict) -> dict:
        try:
            response = self.session.get(self._url(path), params=params, headers=self._headers(), timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise TemplateApiError("network", f"network error: {type(exc).__name__}") from exc
        return self._read(response)

    def _post(self, path: str, body: dict) -> dict:
        try:
            response = self.session.post(self._url(path), json=body, headers=self._headers(), timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise TemplateApiError("network", f"network error: {type(exc).__name__}") from exc
        return self._read(response)

    def waba_name(self) -> str:
        return str(self._get(conf.waba_id(), {"fields": "name"}).get("name") or "")

    def list_templates(self) -> list[dict]:
        """Every template of the WABA. Pages by cursor: Meta's ready-made next link is never followed or stored."""
        rows: list[dict] = []
        after = ""
        for _ in range(MAX_PAGES):
            params = {"fields": FIELDS, "limit": PAGE_SIZE}
            if after:
                params["after"] = after
            data = self._get(f"{conf.waba_id()}/message_templates", params)
            rows += [r for r in data.get("data") or [] if isinstance(r, dict)]
            paging = data.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after") or ""
            if not (paging.get("next") and after):
                return rows
        raise TemplateApiError("too_many_pages", f"More than {MAX_PAGES} pages of templates")

    def create_template(self, payload: dict) -> dict:
        return self._post(f"{conf.waba_id()}/message_templates", payload)

    def edit_template(self, template_id: str, components: list[dict]) -> dict:
        """Meta's way to correct a rejected template: a new one with the same name and language is refused."""
        return self._post(str(template_id), {"components": components})
