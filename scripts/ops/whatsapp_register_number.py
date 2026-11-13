"""Register a WhatsApp Cloud API phone number with Meta, after a read-only preflight.

    MODE=preflight  GET requests only: the token's type and WhatsApp scopes, the number's verification, name, status,
                    platform and PIN state, and whether it belongs to the configured WABA. Sends and changes nothing.
    MODE=register   The same preflight, then POST {number}/register ONCE, only when the number is verified and not
                    already registered on Cloud API; then the preflight again to confirm.

The Graph API version and the system-user token come from the portal's own WhatsApp settings (iic_booking.whatsapp
.conf), so nothing new is configured. The number's ID and the 6-digit two-step PIN come from the environment (GitHub
secrets). Run logs are public: the token, IDs, PIN and phone numbers are never printed.
"""

from __future__ import annotations

import json
import re

FIELDS = ("verified_name", "code_verification_status", "name_status", "new_name_status", "status", "quality_rating",
          "platform_type", "account_mode", "messaging_limit_tier", "display_phone_number")
OPTIONAL_FIELDS = ("is_pin_enabled",)
WHATSAPP_SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")
TIMEOUT = 30

_LONG_NUMBER = re.compile(r"\+?\d[\d\s().-]{5,}\d")
_TOKEN_LIKE = re.compile(r"\bEA[A-Za-z0-9]{10,}\b")
_URL = re.compile(r"https?://\S+")


class Meta:
    def __init__(self, *, api_version, token, target_id, waba_id="", pin="", http_get, http_post, out=print,
                 base="https://graph.facebook.com", other_secrets=()):
        self.api_version, self.token, self.base = api_version, token, base
        self.target_id, self.waba_id, self.pin = str(target_id or ""), str(waba_id or ""), str(pin or "")
        self.http_get, self.http_post, self.out = http_get, http_post, out
        self.known = [k for k in (token, self.target_id, self.waba_id, self.pin, *other_secrets) if k]

    def clean(self, value) -> str:
        text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        for secret in sorted(self.known, key=len, reverse=True):
            text = text.replace(secret, "[hidden]")
        text = _URL.sub("[url]", text)
        text = _TOKEN_LIKE.sub("[token]", text)
        return _LONG_NUMBER.sub("[number]", text)[:300]

    def _call(self, method, path, **kwargs):
        url = f"{self.base}/{self.api_version}/{path}"
        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            send = self.http_get if method == "GET" else self.http_post
            response = send(url, headers=headers, timeout=TIMEOUT, **kwargs)
            data = response.json()
        except Exception as exc:  # noqa: BLE001 - the text of a network error can hold the URL and token
            return None, f"request failed: {type(exc).__name__}"
        if isinstance(data, dict) and "error" in data:
            error = data["error"] if isinstance(data["error"], dict) else {}
            return None, (f"http={response.status_code} code={error.get('code')} subcode={error.get('error_subcode')} "
                          f"{self.clean(error.get('message', ''))}")
        return data, None

    def get(self, path, params=None):
        return self._call("GET", path, params=params or {})

    # --- preflight ----------------------------------------------------------------------------------------------------

    def preflight(self) -> dict:
        facts = {"token_ok": False, "scopes_ok": False, "number": {}, "in_waba": None}
        token, error = self.get("debug_token", {"input_token": self.token})
        if error:
            self.out(f"token: could not be checked ({error})")
        else:
            data = (token or {}).get("data") or {}
            scopes = set(data.get("scopes") or [])
            facts["token_ok"] = bool(data.get("is_valid"))
            facts["scopes_ok"] = all(s in scopes for s in WHATSAPP_SCOPES)
            self.out(f"token: valid={data.get('is_valid')} type={data.get('type')} "
                     f"expires={'never' if not data.get('expires_at') else 'yes'}")
            for scope in WHATSAPP_SCOPES:
                self.out(f"  scope {scope}: {'yes' if scope in scopes else 'NO'}")
        number, error = self.get(self.target_id, {"fields": ",".join(FIELDS + OPTIONAL_FIELDS)})
        if error and "nonexisting field" in error.lower():
            number, error = self.get(self.target_id, {"fields": ",".join(FIELDS)})
        if error:
            self.out(f"number: not readable ({error})")
            return facts
        facts["number"] = number or {}
        for field in FIELDS + OPTIONAL_FIELDS:
            value = facts["number"].get(field)
            if field == "display_phone_number":
                digits = re.sub(r"\D", "", str(value or ""))
                value = f"*****{digits[-2:]}" if len(digits) > 2 else "(none)"
            self.out(f"  {field}: {self.clean(value) if value not in (None, '') else '(not reported)'}")
        if self.waba_id:
            listing, error = self.get(f"{self.waba_id}/phone_numbers", {"fields": "id", "limit": 100})
            if error:
                self.out(f"WABA membership: not checked ({error})")
            else:
                ids = {str(row.get("id")) for row in (listing or {}).get("data") or []}
                facts["in_waba"] = self.target_id in ids
                self.out(f"number belongs to the configured WABA: {'yes' if facts['in_waba'] else 'NO'}")
        return facts

    @staticmethod
    def already_registered(number: dict) -> bool:
        return str(number.get("platform_type", "")).upper() == "CLOUD_API" and str(number.get("status", "")).upper() == "CONNECTED"

    @staticmethod
    def verified(number: dict) -> bool:
        return str(number.get("code_verification_status", "")).upper() == "VERIFIED"

    # --- register -----------------------------------------------------------------------------------------------------

    def run(self, mode: str) -> int:
        if not self.target_id.isdigit():
            self.out("The phone number ID secret is missing or not numeric.")
            return 2
        self.out(f"Graph API version: {self.api_version}")
        facts = self.preflight()
        number = facts["number"]
        if not number:
            return 1
        registered, verified = self.already_registered(number), self.verified(number)
        problems = []
        if not facts["token_ok"]:
            problems.append("the token is not valid")
        if not facts["scopes_ok"]:
            problems.append("the token lacks whatsapp_business_management / whatsapp_business_messaging")
        if not verified:
            problems.append("the number's ownership is not VERIFIED (complete the SMS or voice code in WhatsApp Manager)")
        if facts["in_waba"] is False:
            problems.append("the number is not in the configured WABA")
        self.out(f"already registered on Cloud API: {'yes' if registered else 'no'}")
        self.out("eligible to register: " + ("yes" if not problems and not registered else "no"))
        for problem in problems:
            self.out(f"  - {problem}")
        if mode != "register":
            return 0
        if registered:
            self.out("Not sent: the number is already registered. Registration is never repeated.")
            return 0
        if problems:
            self.out("Not sent: fix the points above first.")
            return 2
        if not re.fullmatch(r"\d{6}", self.pin):
            self.out("Not sent: the PIN secret must be exactly 6 digits.")
            return 2
        body, error = self._call("POST", f"{self.target_id}/register",
                                 json={"messaging_product": "whatsapp", "pin": self.pin})
        if error:
            self.out(f"register: REFUSED ({error})")
            return 1
        self.out(f"register: success={bool((body or {}).get('success'))}")
        self.out("--- after registration")
        after = self.preflight()["number"]
        self.out(f"registered on Cloud API now: {'yes' if self.already_registered(after) else 'not yet (Meta may take a minute)'}")
        return 0 if (body or {}).get("success") else 1


if __name__ == "__main__":
    import os
    import sys

    import django

    django.setup()
    import requests

    from iic_booking.whatsapp import conf

    meta = Meta(api_version=conf.api_version(), token=conf.access_token(), target_id=os.environ.get("TARGET_ID", ""),
                waba_id=os.environ.get("WABA_ID", "") or conf.waba_id(), pin=os.environ.get("PIN", ""),
                http_get=requests.get, http_post=requests.post, base=conf.GRAPH_BASE,
                other_secrets=[conf.app_secret(), conf.verify_token(), conf.phone_number_id()])
    sys.exit(meta.run(os.environ.get("MODE", "preflight")))
