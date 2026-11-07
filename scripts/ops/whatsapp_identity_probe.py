"""READ-ONLY: what Meta reports about the WhatsApp sender's identity (.github/workflows/probe-whatsapp-identity.yml).

GET requests only: it sends nothing and changes no name, registration, setting or record. Its output goes to a public
run log, so it prints no token, Phone Number ID, WABA ID, business ID or full phone number. Senders are told apart by a
label and the last two digits of the number Meta returns.

A WABA counts as linked to a sender only when that WABA's own number list contains the sender. Everything else only
suggests candidates: the WABA and BUSINESS entities in health_status (Meta calls them nodes "involved" in a request, not
parents), a business's WABA lists, and the token's scopes (Meta does not say those ids are WABAs, and leaves them out when
a permission covers everything). Being able to read a number with the token proves nothing about its WABA.
"""

from __future__ import annotations

import json
import re

FIELDS = (
    "id", "display_phone_number", "verified_name", "name_status", "new_name_status", "new_display_name", "status",
    "quality_rating", "code_verification_status", "search_visibility", "platform_type", "messaging_limit_tier",
    "account_mode", "is_official_business_account", "official_business_account", "throughput", "last_onboarded_time",
    "is_pin_enabled", "is_preverified_number", "display_name_status",
)
HEALTH_FIELD = "health_status"
LIST_FIELDS = "id,display_phone_number,verified_name,name_status,status,quality_rating,code_verification_status"
LIST_FALLBACK = "id,display_phone_number,verified_name,quality_rating"
WABA_FIELDS = "name,account_review_status,business_verification_status,ownership_type,country"
TEMPLATE_FIELDS = "name,language,status,category,rejected_reason"
TEMPLATE_PAGES = 20
BUSINESS_EDGES = ("owned_whatsapp_business_accounts", "client_whatsapp_business_accounts")
WHATSAPP_SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")
EXPECTED_NAME = "Indian Institute of Technology Roorkee"
CALLING_ONLY_FIELD = "can_receive_call_sip"
TIMEOUT = 25
LIMIT = 300

_LONG_NUMBER = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_TOKEN_LIKE = re.compile(r"\bEA[A-Za-z0-9]{10,}\b")
_MESSAGE_ID = re.compile(r"wamid\.[A-Za-z0-9=_+/-]+")
_URL = re.compile(r"https?://\S+")
_ERROR_CODE_KEYS = ("error_code", "code", "error_subcode", "subcode")


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
        self.entities = {"WABA": {}, "BUSINESS": {}}  # entity id -> labels of the senders whose health report named it

    def clean(self, value, limit=LIMIT) -> str:
        """Everything from Meta passes through here before it is printed."""
        text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        for secret in sorted((k for k in self.known if k), key=len, reverse=True):
            text = text.replace(secret, "[hidden]")
        text = _URL.sub("[url]", text)
        text = _MESSAGE_ID.sub("[message id]", text)
        text = _TOKEN_LIKE.sub("[token]", text)
        text = _LONG_NUMBER.sub(lambda m: m.group() if _DATE.fullmatch(m.group()) else "[number]", text)
        return text[:limit] if limit else text

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

    def flatten(self, prefix, value):
        """Every field of a nested reply, one line each and untruncated, so nothing Meta says is lost. Ids stay hidden."""
        if isinstance(value, dict):
            for key in sorted(value):
                if key == "id":
                    self.out(f"{prefix}.id: [hidden]")
                elif key in _ERROR_CODE_KEYS and re.fullmatch(r"\d{1,8}", str(value[key])):
                    self.out(f"{prefix}.{key}: {value[key]}")
                else:
                    self.flatten(f"{prefix}.{self.clean(key)}", value[key])
        elif isinstance(value, list):
            if not value:
                self.out(f"{prefix}: (empty)")
            for index, item in enumerate(value):
                self.flatten(f"{prefix}[{index}]", item)
        else:
            self.out(f"{prefix}: {self.clean(value, limit=None)}")

    def report_health(self, label, health) -> None:
        """Print the whole health report, then what it means. Remembers named WABAs and businesses as candidates only."""
        if not isinstance(health, dict):
            self.out(f"  {HEALTH_FIELD}: {self.clean(health, limit=None)}")
            return
        entities = [e for e in health.get("entities") or [] if isinstance(e, dict)]
        for entity in entities:
            kind, entity_id = str(entity.get("entity_type") or ""), str(entity.get("id") or "")
            if entity_id:
                self.known.append(entity_id)
                if kind in self.entities:
                    self.entities[kind].setdefault(entity_id, []).append(label)
        self.flatten(f"  {HEALTH_FIELD}", health)
        blocked_messaging, limited_messaging, codes = [], [], []
        for entity in entities:
            kind = self.clean(entity.get("entity_type"))
            if entity.get("can_send_message") == "BLOCKED":
                blocked_messaging.append(kind)
            elif entity.get("can_send_message") == "LIMITED":
                limited_messaging.append(kind)
            for err in entity.get("errors") or []:
                if isinstance(err, dict):
                    codes.append(f"{self.clean(err.get('error_code'))} on {kind}")
        sip_blocked = [self.clean(e.get("entity_type")) for e in entities if e.get(CALLING_ONLY_FIELD) == "BLOCKED"]
        self.out(f"  health summary: overall={self.clean(health.get('can_send_message'))} "
                 f"entities_blocking_messaging={blocked_messaging or 'none'} entities_limiting_messaging={limited_messaging or 'none'} "
                 f"entities_blocking_sip_calls={sip_blocked or 'none'} error_codes={codes or 'none'}")
        if health.get("can_send_message") == "BLOCKED" and not blocked_messaging:
            self.out("  health note: overall is BLOCKED, but no entity reports can_send_message=BLOCKED")

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
        data, error = self.get(phone_id, {"fields": HEALTH_FIELD})
        if error:
            self.out(f"  {HEALTH_FIELD}: {error['text']}")
        elif isinstance(data, dict) and HEALTH_FIELD in data:
            self.report_health(label, data[HEALTH_FIELD])
        else:
            self.out(f"  {HEALTH_FIELD}: no value from Meta")

    def token_candidates(self):
        data, error = self.get("debug_token", {"input_token": self.cfg.token})
        if error:
            self.out(f"token lookup: {error['text']}")
            return []
        info = data.get("data") if isinstance(data, dict) else None
        info = info if isinstance(info, dict) else {}
        self.out(f"token: valid={info.get('is_valid')} type={self.clean(info.get('type'))} scopes={self.clean(info.get('scopes'))}")
        found = []
        for scope in info.get("granular_scopes") or []:
            targets = [str(t) for t in scope.get("target_ids") or []]
            self.out(f"  scope {self.clean(scope.get('scope'))}: {len(targets)} target(s) listed")
            if scope.get("scope") in WHATSAPP_SCOPES:
                found += targets
        self.known += found
        return found

    def business_candidates(self):
        found = []
        for index, business in enumerate(sorted(self.entities["BUSINESS"]), 1):
            for edge in BUSINESS_EDGES:
                data, error = self.get(f"{business}/{edge}", {"fields": "id"})
                if error:
                    self.out(f"business {index} {edge}: {error['text']}")
                    continue
                rows = (data or {}).get("data") or []
                ids = [str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")]
                self.known += ids
                self.out(f"business {index} {edge}: {len(ids)} WABA(s)")
                found += ids
        return found

    def waba_candidates(self):
        """(id, where it came from) in a stable order. Nothing here is treated as proof."""
        if self.cfg.waba_id:
            return [(self.cfg.waba_id, "secret")]
        ordered = {}
        for source, ids in (("health_status", sorted(self.entities["WABA"])), ("token scopes", self.token_candidates()),
                            ("business lists", self.business_candidates())):
            for waba in ids:
                ordered.setdefault(waba, source)
        return list(ordered.items())

    def report_templates(self, waba) -> None:
        """Every template of a WABA: name, status, category, language and rejection reason. Bodies are not printed."""
        rows, after, error = [], "", None
        for _ in range(TEMPLATE_PAGES):
            params = {"fields": TEMPLATE_FIELDS, "limit": 100}
            if after:
                params["after"] = after
            data, error = self.get(f"{waba}/message_templates", params)
            if error:
                break
            rows += (data or {}).get("data") or []
            after = (((data or {}).get("paging") or {}).get("cursors") or {}).get("after")
            if not (((data or {}).get("paging") or {}).get("next") and after):
                break
        if error:
            self.out(f"  templates: {error['text']}")
            return
        self.out(f"  templates: {len(rows)}")
        for row in rows:
            fields = " ".join(f"{k}={self.clean(row.get(k))}" for k in ("name", "language", "status", "category", "rejected_reason"))
            self.out(f"  template {fields}")

    def check_waba(self) -> tuple[str, str, str]:
        """(association of the investigated sender, of the configured sender, whether they share a WABA)."""
        target, configured = self.cfg.target_id, self.cfg.configured_id
        self.out("--- WABA")
        candidates = self.waba_candidates()
        self.out(f"candidates: {len(candidates)} ({', '.join(sorted({s for _, s in candidates})) or 'no source gave one'})")
        needed = "The WABA ID is needed from WhatsApp Manager"
        if not candidates:
            verdict = f"NOT DETERMINED: no WABA candidate. {needed}"
            return verdict, verdict, "unknown"
        readable = 0
        holds_target, holds_configured = [], []
        for index, (waba, source) in enumerate(candidates, 1):
            data, error = self.get(waba, {"fields": WABA_FIELDS})
            details = error["text"] if error else self.clean({k: v for k, v in (data or {}).items() if k != "id"})
            self.out(f"waba {index} (from {source}): {details}")
            if not error and isinstance(data, dict) and "name" in data:
                self.out(f"  name_matches_institute: {data.get('name') == EXPECTED_NAME}")
            data, error = self.get(waba, {"fields": HEALTH_FIELD})
            if error:
                self.out(f"  {HEALTH_FIELD}: {error['text']}")
            elif isinstance(data, dict) and isinstance(data.get(HEALTH_FIELD), dict):
                self.report_health(f"waba {index}", data[HEALTH_FIELD])
            self.report_templates(waba)
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
                holds_target.append(index)
            if has_configured:
                holds_configured.append(index)

        def verdict(holders):
            if holders:
                return f"CONFIRMED: listed by waba {holders[0]}"
            if readable:
                return f"MISMATCH: not in the {readable} WABA number list(s) that could be read"
            if any(waba in self.entities["WABA"] for waba, _ in candidates):
                return f"NOT CONFIRMED: a health report names a WABA, but its number list could not be read. {needed}"
            return f"NOT DETERMINED: no WABA number list could be read. {needed}"

        same = str(bool(set(holds_target) & set(holds_configured))) if holds_target and holds_configured else "unknown"
        return verdict(holds_target), verdict(holds_configured), same

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
            investigated, configured, same = self.check_waba()
            self.out(f"waba_association: {investigated}")
            if cfg.configured_id and cfg.configured_id != cfg.target_id:
                self.out(f"waba_association_configured: {configured}")
                self.out(f"senders_share_a_waba: {same}")
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
