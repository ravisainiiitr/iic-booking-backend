"""Show or set the WhatsApp Business profile of the portal's sender (the number in WHATSAPP_PHONE_NUMBER_ID).

    MODE=show   GET only: the profile as WhatsApp users see it (about, description, address, email, websites, vertical,
                whether a photo is set). Changes nothing.
    MODE=apply  Uploads the profile photo (Meta's resumable upload, for the token's app) and sets the text fields from
                PROFILE_JSON, then shows the profile again.

The Graph API version, token and sender come from the portal's own WhatsApp settings. Run logs are public: the token,
IDs and upload handles are never printed.
"""

from __future__ import annotations

import json
import re

FIELDS = "about,address,description,email,profile_picture_url,websites,vertical"
LIMITS = {"about": 139, "description": 512, "address": 256, "email": 128}
VERTICALS = {"UNDEFINED", "OTHER", "AUTO", "BEAUTY", "APPAREL", "EDU", "ENTERTAIN", "EVENT_PLAN", "FINANCE", "GROCERY",
             "GOVT", "HOTEL", "HEALTH", "NONPROFIT", "PROF_SERVICES", "RETAIL", "TRAVEL", "RESTAURANT", "NOT_A_BIZ"}
TIMEOUT = 60
_TOKEN_LIKE = re.compile(r"\bEA[A-Za-z0-9]{10,}\b")
_HANDLE = re.compile(r"\b\d:[A-Za-z0-9+/=_-]{8,}")


def validate(profile: dict) -> list[str]:
    problems = []
    for key, limit in LIMITS.items():
        value = profile.get(key, "")
        if not isinstance(value, str) or len(value) > limit:
            problems.append(f"{key} must be text of at most {limit} characters")
    websites = profile.get("websites", [])
    if not isinstance(websites, list) or len(websites) > 2 or not all(str(w).startswith(("http://", "https://")) for w in websites):
        problems.append("websites must be at most 2 http(s) addresses")
    if profile.get("vertical", "EDU") not in VERTICALS:
        problems.append("vertical is not one Meta accepts")
    return problems


class Profile:
    def __init__(self, *, api_version, token, phone_id, http_get, http_post, out=print, base="https://graph.facebook.com",
                 other_secrets=()):
        self.api_version, self.token, self.phone_id, self.base = api_version, token, str(phone_id or ""), base
        self.http_get, self.http_post, self.out = http_get, http_post, out
        self.known = [k for k in (token, self.phone_id, *other_secrets) if k]

    def clean(self, value) -> str:
        text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
        for secret in sorted(self.known, key=len, reverse=True):
            text = text.replace(secret, "[hidden]")
        text = _HANDLE.sub("[handle]", _TOKEN_LIKE.sub("[token]", text))
        return text[:600]

    def _json(self, response):
        try:
            data = response.json()
        except Exception:  # noqa: BLE001
            return None, f"http={getattr(response, 'status_code', '?')} (no JSON)"
        if isinstance(data, dict) and "error" in data:
            error = data["error"] if isinstance(data["error"], dict) else {}
            return None, (f"http={response.status_code} code={error.get('code')} subcode={error.get('error_subcode')} "
                          f"{self.clean(error.get('message', ''))}")
        return data, None

    def call(self, method, path, *, auth="Bearer", headers=None, **kwargs):
        url = f"{self.base}/{self.api_version}/{path}"
        all_headers = {"Authorization": f"{auth} {self.token}", **(headers or {})}
        try:
            send = self.http_get if method == "GET" else self.http_post
            return self._json(send(url, headers=all_headers, timeout=TIMEOUT, **kwargs))
        except Exception as exc:  # noqa: BLE001 - a network error's text can hold the URL and token
            return None, f"request failed: {type(exc).__name__}"

    def show(self) -> bool:
        body, error = self.call("GET", f"{self.phone_id}/whatsapp_business_profile", params={"fields": FIELDS})
        if error:
            self.out(f"profile: not readable ({error})")
            return False
        row = ((body or {}).get("data") or [{}])[0]
        for key in ("about", "description", "address", "email", "websites", "vertical"):
            value = row.get(key)
            self.out(f"  {key}: {self.clean(value) if value not in (None, '', []) else '(not set)'}")
        self.out(f"  profile photo: {'set' if row.get('profile_picture_url') else 'not set'}")
        return True

    def upload_photo(self, image: bytes, mime: str = "image/jpeg") -> str | None:
        debug, error = self.call("GET", "debug_token", params={"input_token": self.token})
        app_id = str(((debug or {}).get("data") or {}).get("app_id") or "")
        if error or not app_id:
            self.out(f"photo: the token's app could not be read ({error or 'no app_id'})")
            return None
        self.known.append(app_id)
        session, error = self.call("POST", f"{app_id}/uploads", params={"file_length": len(image), "file_type": mime})
        session_id = str((session or {}).get("id") or "")
        if error or not session_id:
            self.out(f"photo: upload session refused ({error or 'no id'})")
            return None
        self.known.append(session_id)
        done, error = self.call("POST", session_id, auth="OAuth", headers={"file_offset": "0"}, data=image)
        handle = str((done or {}).get("h") or "")
        if error or not handle:
            self.out(f"photo: upload refused ({error or 'no handle'})")
            return None
        self.known.append(handle)
        self.out(f"photo: uploaded ({len(image)} bytes)")
        return handle

    def run(self, mode: str, profile: dict | None = None, image: bytes | None = None) -> int:
        if not self.phone_id.isdigit():
            self.out("The portal's WhatsApp sender (WHATSAPP_PHONE_NUMBER_ID) is not configured.")
            return 2
        self.out(f"Graph API version: {self.api_version}")
        self.out("--- current profile")
        if not self.show():
            return 1
        if mode != "apply":
            return 0
        profile = dict(profile or {})
        problems = validate(profile)
        if problems:
            for problem in problems:
                self.out(f"Not sent: {problem}")
            return 2
        payload = {"messaging_product": "whatsapp", **profile}
        if image:
            handle = self.upload_photo(image)
            if handle is None:
                return 1
            payload["profile_picture_handle"] = handle
        body, error = self.call("POST", f"{self.phone_id}/whatsapp_business_profile", json=payload)
        if error:
            self.out(f"profile update: REFUSED ({error})")
            return 1
        self.out(f"profile update: success={bool((body or {}).get('success'))}")
        self.out("--- profile now")
        self.show()
        return 0 if (body or {}).get("success") else 1


if __name__ == "__main__":
    import os
    import sys
    from pathlib import Path

    import django

    django.setup()
    import requests

    from iic_booking.whatsapp import conf

    mode = os.environ.get("MODE", "show")
    profile = json.loads(os.environ.get("PROFILE_JSON") or "{}")
    image_path = os.environ.get("IMAGE_PATH", "")
    image = Path(image_path).read_bytes() if mode == "apply" and image_path and Path(image_path).is_file() else None
    runner = Profile(api_version=conf.api_version(), token=conf.access_token(), phone_id=conf.phone_number_id(),
                     http_get=requests.get, http_post=requests.post, base=conf.GRAPH_BASE,
                     other_secrets=[conf.app_secret(), conf.verify_token(), conf.waba_id()])
    sys.exit(runner.run(mode, profile, image))
