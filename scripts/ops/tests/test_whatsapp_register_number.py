"""WhatsApp number registration, against a fake Meta API. No Django, database or network.

    python -m pytest scripts/ops/tests/test_whatsapp_register_number.py -p no:django -o addopts="" --import-mode=importlib
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("whatsapp_register_number", ROOT / "scripts/ops/whatsapp_register_number.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TOKEN, TARGET, WABA, PIN = "EAAFakeTokenValueNotReal123456789", "1000000000000111", "3000000000000333", "246810"
NUMBER = "+91 99999 00011"
SENSITIVE = (TOKEN, TARGET, WABA, PIN, "99999", "graph.facebook")


class Response:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code

    def json(self):
        return self.body


def fake(number, *, scopes=module.WHATSAPP_SCOPES, in_waba=True, register=None):
    calls = {"post": []}
    token = {"data": {"is_valid": True, "type": "SYSTEM_USER", "expires_at": 0, "scopes": list(scopes)}}

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("/debug_token"):
            return Response(token)
        if url.endswith(f"/{WABA}/phone_numbers"):
            return Response({"data": [{"id": TARGET}] if in_waba else [{"id": "9"}]})
        if url.endswith(f"/{TARGET}"):
            return Response(dict(number, display_phone_number=NUMBER))
        return Response({"error": {"code": 100, "message": "unknown"}}, 400)

    def post(url, json=None, headers=None, timeout=None):
        calls["post"].append((url, json))
        return Response(register or {"success": True})

    return get, post, calls


def run(mode, number, **kw):
    lines = []
    get, post, calls = fake(number, **{k: v for k, v in kw.items() if k in ("scopes", "in_waba", "register")})
    meta = module.Meta(api_version="v21.0", token=TOKEN, target_id=TARGET, waba_id=WABA, pin=kw.get("pin", PIN),
                       http_get=get, http_post=post, out=lines.append)
    code = meta.run(mode)
    text = "\n".join(lines)
    for secret in SENSITIVE:
        assert secret not in text, secret
    return code, text, calls


PENDING = {"code_verification_status": "VERIFIED", "status": "PENDING", "platform_type": "NOT_APPLICABLE",
           "verified_name": "Indian Institute of Technology Roorkee", "is_pin_enabled": False}
REGISTERED = dict(PENDING, status="CONNECTED", platform_type="CLOUD_API")


def test_preflight_sends_nothing_and_reports_eligibility():
    code, text, calls = run("preflight", PENDING)
    assert code == 0 and not calls["post"]
    assert "eligible to register: yes" in text and "scope whatsapp_business_management: yes" in text
    assert "*****11" in text


def test_register_posts_once_with_the_pin_and_whatsapp_product():
    code, text, calls = run("register", PENDING)
    assert code == 0 and len(calls["post"]) == 1
    url, body = calls["post"][0]
    assert url.endswith(f"/v21.0/{TARGET}/register")
    assert body == {"messaging_product": "whatsapp", "pin": PIN}
    assert "register: success=True" in text


def test_an_already_registered_number_is_never_registered_again():
    code, text, calls = run("register", REGISTERED)
    assert code == 0 and not calls["post"]
    assert "already registered" in text


def test_unverified_number_missing_scope_or_wrong_waba_is_not_sent():
    for number, kw in ((dict(PENDING, code_verification_status="NOT_VERIFIED"), {}),
                       (PENDING, {"scopes": ("whatsapp_business_messaging",)}),
                       (PENDING, {"in_waba": False})):
        code, _, calls = run("register", number, **kw)
        assert code == 2 and not calls["post"]


def test_a_bad_pin_is_not_sent_and_refusals_hide_secrets():
    code, _, calls = run("register", PENDING, pin="12ab")
    assert code == 2 and not calls["post"]
    refusal = {"error": {"code": 133005, "message": f"Two step verification PIN Mismatch for {TARGET} pin {PIN}"}}
    code, text, _ = run("register", PENDING, register=refusal)
    assert code == 1 and "REFUSED" in text and "133005" in text


def test_workflow_takes_ids_and_pin_from_secrets_and_needs_confirmation():
    workflow = yaml.safe_load((ROOT / ".github/workflows/whatsapp-register-number.yml").read_text(encoding="utf-8"))
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"mode", "confirm"}
    steps = workflow["jobs"]["run"]["steps"]
    env = next(s for s in steps if s.get("name") == "Ask Meta")["env"]
    assert "secrets.WHATSAPP_REGISTER_PHONE_NUMBER_ID" in env["TARGET_ID"]
    assert "secrets.WHATSAPP_REGISTER_PIN" in env["PIN"] and "mode == 'register'" in env["PIN"]
    assert "REGISTER" in next(s for s in steps if s.get("name") == "Require confirmation to register")["if"]
