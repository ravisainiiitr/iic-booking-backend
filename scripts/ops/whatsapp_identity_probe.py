"""READ-ONLY: what Meta reports about the WhatsApp sender's identity (.github/workflows/probe-whatsapp-identity.yml).

GET requests only: it sends nothing and changes no name, registration, setting or record. Its output goes to a public
run log, so it prints no token, Phone Number ID, WABA ID or full phone number. Senders are told apart by a label and the
last two digits of the number Meta returns.

A WABA counts as linked to a sender only when that WABA's own number list contains the sender. The token's scopes only
suggest candidates: Meta does not say those ids are WABAs, and leaves them out when a permission covers everything.
"""

from __future__ import annotations

import json
import re

FIELDS = (
    "id", "display_phone_number", "verified_name", "name_status", "new_name_status", "new_display_name", "status",
    "quality_rating", "code_verification_status", "search_visibility", "platform_type", "messaging_limit_tier",
    "account_mode", "is_official_business_account", "official_business_account", "health_status", "throughput",
)
LIST_FIELDS = "id,display_phone_number,verified_name,name_status,status,quality_rating,code_verification_status"
LIST_FALLBACK = "id,display_phone_number,verified_name,quality_rating"
WHATSAPP_SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")
TIMEOUT = 25

_LONG_NUMBER = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_TOKEN_LIKE = re.compile(r"\bEA[A-Za-z0-9]{10,}\b")
_MESSAGE_ID = re.compile(r"wamid\.[A-Za-z0-9=_+/-]+")
_URL = re.compile(r"https?://\S+")


class Config:
    def __init__(self, *, api_version, token, configured_id, target_id, waba_id="", other_secrets=(), base="https://graph.facebook.com"):
        self.api_version, self.token, self.base = api_version, token, base
        self.configured_id, self.target_id, self.waba_id = str(configured_id or ""), str(target_id or ""), str(waba_id or "")
        self.other_secrets = [s for s in other_secrets if s]


class TokenInvalid(Exception):
    pass


class Probe:
    def __init__(self, cfg, http_get, out=print):
        self.cfg, self.http_get, self.out = cfg, http_get, out
        self.known = [cfg.token, cfg.configured_id, cfg.target_id, cfg.waba_id, *cfg.other_secrets]

    def clean(self, value) -> str:
        """Everything from Meta passes through here before it is printed."""
        text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        for secret in sorted((k for k in self.known if k), key=len, reverse=True):
            text = text.replace(secret, "[hidden]")
        text = _URL.sub("[url]", text)
        text = _MESSAGE_ID.sub("[message id]", text)
        text = _TOKEN_LIKE.sub("[token]", text)
        return _LONG_NUMBER.sub("[number]", text)[:300]

    def get(self, path, params=None):
        """(data, error). A failure prints only its type: the text of a network error can hold the URL and token."""
        cfg = self.cfg
        try:
            response = self.http_get(f"{cfg.base}/{cfg.api_version}/{path}", params=params or {},
                                     headers={"Authorization": f"Bearer {cfg.token}"}, timeout=TIMEOUT)
            data = response.json()
        except Exception as exc:  # noqa: BLE001
            return None, {"kind": "request_failed", "text": f"request failed: {type(exc).__name__}"}
        if isinstance(data, dict) and "error" in data:
            error = data["error"] if isinstance(data["error"], dict) else {}
            kind = _classify(error)
            if kind == "token_invalid_or_expired":
                raise TokenInvalid()
            text = f"{kind} http={response.status_code} code={error.get('code')} subcode={error.get('error_subcode')} {self.clean(error.get('message', ''))}"
            return None, {"kind": kind, "text": text}
        return data, None

    def show(self, field, value) -> str:
        if field == "id":
            return "[hidden]"
        if field == "display_phone_number":
            return mask_number(value)
        return self.clean(value)

    def inspect(self, label, phone_id) -> None:
        self.out(f"--- {label}")
        for field in FIELDS:
            data, error = self.get(phone_id, {"fields": field})
            if error and error["kind"] == "not_accessible":
                self.out(f"  NOT ACCESSIBLE with this token: {error['text']}")
                return
            if error:
                self.out(f"  {field}: {error['text']}")
            elif isinstance(data, dict) and field in data:
                self.out(f"  {field}: {self.show(field, data[field])}")
            else:
                self.out(f"  {field}: no value from Meta (not evidence of approval or rejection)")

    def waba_candidates(self):
        if self.cfg.waba_id:
            return [self.cfg.waba_id], "secret"
        data, error = self.get("debug_token", {"input_token": self.cfg.token})
        if error:
            self.out(f"token lookup: {error['text']}")
            return [], "token lookup failed"
        info = data.get("data") if isinstance(data, dict) else None
        info = info if isinstance(info, dict) else {}
        self.out(f"token: valid={info.get('is_valid')} type={self.clean(info.get('type'))} scopes={self.clean(info.get('scopes'))}")
        found = []
        for scope in info.get("granular_scopes") or []:
            targets = scope.get("target_ids") or []
            self.out(f"  scope {self.clean(scope.get('scope'))}: {len(targets)} target(s) listed")
            if scope.get("scope") in WHATSAPP_SCOPES:
                found += [str(t) for t in targets]
        self.known += found
        return sorted(set(found)), "token scopes"

    def check_waba(self) -> str:
        target, configured = self.cfg.target_id, self.cfg.configured_id
        self.out("--- WABA")
        candidates, source = self.waba_candidates()
        self.out(f"candidates: {len(candidates)} from {source}")
        if not candidates:
            return "NOT DETERMINED: no WABA candidate. The WABA ID is needed from WhatsApp Manager"
        readable, holders = 0, []
        for index, waba in enumerate(candidates, 1):
            data, error = self.get(waba, {"fields": "name,account_review_status,business_verification_status"})
            details = error["text"] if error else self.clean({k: v for k, v in (data or {}).items() if k != "id"})
            self.out(f"waba {index}: {details}")
            data, error = self.get(f"{waba}/phone_numbers", {"fields": LIST_FIELDS})
            if error:
                self.out(f"  list with name_status: {error['text']}")
                data, error = self.get(f"{waba}/phone_numbers", {"fields": LIST_FALLBACK})
            if error:
                self.out(f"  list: {error['text']}")
                continue
            readable += 1
            rows = (data or {}).get("data") or []
            has_target = any(str(r.get("id")) == target for r in rows)
            has_configured = any(str(r.get("id")) == configured for r in rows)
            self.out(f"  numbers: {len(rows)} has_investigated={has_target} has_configured={has_configured}")
            for row in rows:
                row_id = str(row.get("id"))
                role = "investigated" if row_id == target else "configured" if row_id == configured else "other"
                fields = " ".join(f"{k}={self.show(k, v)}" for k, v in sorted(row.items()) if k != "id")
                self.out(f"  number ({role}): {fields}")
            if has_target:
                holders.append(index)
        if holders:
            return f"CONFIRMED: investigated sender is listed by waba {holders[0]}"
        if readable:
            return f"MISMATCH: investigated sender is not in the {readable} WABA(s) readable from {source}"
        return "NOT DETERMINED: no WABA number list could be read. The WABA ID is needed from WhatsApp Manager"

    def run(self, enabled) -> int:
        cfg = self.cfg
        if not cfg.target_id:
            self.out("STOP: WHATSAPP_DIAGNOSTIC_PHONE_NUMBER_ID is not set")
            return 1
        self.out(f"graph_api_version: {self.clean(cfg.api_version)}")
        self.out(f"sending_enabled: {bool(enabled)}")
        self.out(f"configured_sender_is_the_investigated_sender: {cfg.configured_id == cfg.target_id}")
        try:
            self.inspect("sender A (investigated)", cfg.target_id)
            if cfg.configured_id and cfg.configured_id != cfg.target_id:
                self.inspect("sender B (configured on production)", cfg.configured_id)
            self.out(f"waba_association: {self.check_waba()}")
        except TokenInvalid:
            self.out("STOP: Meta says the access token is invalid or expired. Nothing further was requested")
            return 2
        self.out("done: GET requests only")
        return 0


def mask_number(number) -> str:
    digits = re.sub(r"\D", "", str(number or ""))
    return f"*****{digits[-2:]}" if len(digits) > 2 else "(none)"


def _classify(error) -> str:
    code, subcode, message = error.get("code"), error.get("error_subcode"), str(error.get("message", "")).lower()
    if code == 190:
        return "token_invalid_or_expired"
    if subcode == 33 or "does not exist" in message or "missing permissions" in message:
        return "not_accessible"
    if code == 100 and ("nonexisting field" in message or "tried accessing" in message):
        return "field_unsupported"
    if code in (3, 10, 200):
        return "permission_denied"
    return "meta_error"


if __name__ == "__main__":
    import os
    import sys

    import django

    django.setup()
    import requests
    from django.conf import settings

    from iic_booking.whatsapp import conf

    config = Config(api_version=conf.api_version(), token=conf.access_token(), configured_id=conf.phone_number_id(),
                    target_id=os.environ.get("TARGET_ID", ""), waba_id=os.environ.get("WABA_ID", ""),
                    other_secrets=[conf.app_secret(), conf.verify_token()], base=conf.GRAPH_BASE)
    sys.exit(Probe(config, requests.get).run(bool(getattr(settings, "WHATSAPP_ENABLED", False)) and conf.enabled()))
