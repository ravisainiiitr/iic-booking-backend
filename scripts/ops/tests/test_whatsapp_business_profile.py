"""WhatsApp business profile, against a fake Meta API. No Django, database or network.

    python -m pytest scripts/ops/tests/test_whatsapp_business_profile.py -p no:django -o addopts="" --import-mode=importlib
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location("whatsapp_business_profile", ROOT / "scripts/ops/whatsapp_business_profile.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TOKEN, PHONE, APP, SESSION, HANDLE = "EAAFakeTokenValueNotReal123456789", "1000000000000111", "4000000000000444", "upload:MTpfake?sig=x", "4:aGFuZGxlVmFsdWU="
PROFILE = json.loads((ROOT / "scripts/ops/assets/whatsapp_business_profile.json").read_text(encoding="utf-8"))


class Response:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code

    def json(self):
        return self.body


def fake(update=None):
    calls = []
    state = {"row": {"about": "", "vertical": "UNDEFINED"}}

    def get(url, params=None, headers=None, timeout=None):
        calls.append(("GET", url, params, headers, None))
        if url.endswith("/debug_token"):
            return Response({"data": {"app_id": APP}})
        return Response({"data": [state["row"]]})

    def post(url, params=None, headers=None, timeout=None, json=None, data=None):
        calls.append(("POST", url, params, headers, json if json is not None else data))
        if url.endswith(f"/{APP}/uploads"):
            return Response({"id": SESSION})
        if url.endswith(f"/{SESSION}"):
            return Response({"h": HANDLE})
        if update:
            return Response(update, 400)
        state["row"] = {**{k: v for k, v in json.items() if k != "messaging_product"}, "profile_picture_url": "https://x"}
        return Response({"success": True})

    return get, post, calls


def run(mode, image=None, update=None, profile=PROFILE):
    lines = []
    get, post, calls = fake(update)
    code = module.Profile(api_version="v21.0", token=TOKEN, phone_id=PHONE, http_get=get, http_post=post,
                          out=lines.append).run(mode, profile, image)
    text = "\n".join(lines)
    for secret in (TOKEN, PHONE, APP, SESSION, HANDLE, "graph.facebook"):
        assert secret not in text, secret
    return code, text, calls


def test_the_committed_profile_is_within_metas_limits():
    assert module.validate(PROFILE) == []
    assert PROFILE["vertical"] == "EDU" and PROFILE["email"] == "equip@iitr.ac.in"


def test_show_only_reads():
    code, text, calls = run("show")
    assert code == 0 and all(c[0] == "GET" for c in calls)
    assert "profile photo: not set" in text


def test_apply_uploads_the_photo_then_sets_the_profile_with_its_handle():
    code, text, calls = run("apply", image=b"\xff\xd8jpeg")
    assert code == 0
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[0][1].endswith(f"/{APP}/uploads") and posts[0][2] == {"file_length": 6, "file_type": "image/jpeg"}
    assert posts[1][3]["Authorization"].startswith("OAuth ") and posts[1][3]["file_offset"] == "0"
    body = posts[2][4]
    assert posts[2][1].endswith(f"/{PHONE}/whatsapp_business_profile")
    assert body["messaging_product"] == "whatsapp" and body["profile_picture_handle"] == HANDLE
    assert body["about"] == PROFILE["about"] and body["websites"] == PROFILE["websites"]
    assert "profile update: success=True" in text and "profile photo: set" in text


def test_invalid_text_or_a_refusal_is_reported_without_secrets():
    code, _, calls = run("apply", profile=dict(PROFILE, about="x" * 200))
    assert code == 2 and not [c for c in calls if c[0] == "POST"]
    code, text, _ = run("apply", update={"error": {"code": 100, "message": f"Invalid parameter for {PHONE} {TOKEN}"}})
    assert code == 1 and "REFUSED" in text


def test_workflow_needs_confirmation_and_takes_no_ids_as_inputs():
    workflow = yaml.safe_load((ROOT / ".github/workflows/whatsapp-business-profile.yml").read_text(encoding="utf-8"))
    assert set(workflow[True]["workflow_dispatch"]["inputs"]) == {"mode", "confirm"}
    steps = workflow["jobs"]["run"]["steps"]
    assert "PROFILE" in steps[0]["if"]
