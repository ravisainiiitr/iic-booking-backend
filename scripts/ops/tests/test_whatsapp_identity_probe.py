"""The WhatsApp identity probe, against a fake Meta API. No Django, database or network.

    python -m pytest scripts/ops/tests/test_whatsapp_identity_probe.py -p no:django -o addopts="" --import-mode=importlib
"""

from __future__ import annotations

import ast
import importlib.util
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/ops/whatsapp_identity_probe.py"
WORKFLOWS = ROOT / ".github/workflows"

spec = importlib.util.spec_from_file_location("whatsapp_identity_probe", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TOKEN = "EAAFakeTokenValueNotReal123456789"
APP_SECRET, VERIFY_TOKEN = "fake-app-secret-value", "fake-verify-token-value"
TARGET, CONFIGURED, WABA = "1000000000000111", "2000000000000222", "3000000000000333"
TARGET_NUMBER, CONFIGURED_NUMBER = "+1 555-010-0182", "+1 555-010-0180"
SENSITIVE = (TOKEN, APP_SECRET, VERIFY_TOKEN, TARGET, CONFIGURED, WABA, TARGET_NUMBER, CONFIGURED_NUMBER, "555-010",
             "wamid.", "input_token", "graph.facebook")
NAME = "Indian Institute of Technology Roorkee"
UNSUPPORTED = {"error": {"code": 100, "message": "(#100) Tried accessing nonexisting field (x) on node type (Phone)"}}
NOT_ACCESSIBLE = {"error": {"code": 100, "error_subcode": 33, "message": f"Object with ID '{TARGET}' does not exist, cannot be loaded due to missing permissions"}}
EXPIRED = {"error": {"code": 190, "error_subcode": 463, "message": f"Error validating access token: Session has expired {TOKEN}"}}


class Response:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code

    def json(self):
        return self.body


class Meta:
    """Answers like Graph API. Every call is recorded, and the probe can only reach it through a GET."""

    def __init__(self, target=None, configured=None, waba_rows=None, debug=None, waba_ok=True):
        self.calls = []
        self.target = {"id": TARGET, "display_phone_number": TARGET_NUMBER, "verified_name": NAME, "status": "CONNECTED",
                       "name_status": "PENDING_REVIEW", "new_name_status": "NONE", "quality_rating": "GREEN",
                       "code_verification_status": "VERIFIED", "platform_type": "CLOUD_API",
                       "health_status": {"entities": [{"id": TARGET, "can_send_message": "AVAILABLE"}]}} if target is None else target
        self.configured = {"id": CONFIGURED, "display_phone_number": CONFIGURED_NUMBER, "verified_name": "Test Number"} if configured is None else configured
        self.rows = [{"id": TARGET, "display_phone_number": TARGET_NUMBER, "verified_name": NAME}] if waba_rows is None else waba_rows
        self.debug = debug or {"data": {"is_valid": True, "type": "SYSTEM_USER", "scopes": ["whatsapp_business_messaging"],
                                        "granular_scopes": [{"scope": "whatsapp_business_management", "target_ids": [WABA]}]}}
        self.waba_ok = waba_ok

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = params or {}
        path = url.split("/v21.0/", 1)[1]
        self.calls.append(path)
        assert headers == {"Authorization": f"Bearer {TOKEN}"} and timeout
        if path == "debug_token":
            return Response(self.debug, 400 if "error" in self.debug else 200)
        if path == TARGET or path == CONFIGURED:
            node = self.target if path == TARGET else self.configured
            if "error" in node:
                return Response(node, 400)
            field = params["fields"]
            if field in ("search_visibility", "messaging_limit_tier"):
                return Response(UNSUPPORTED, 400)
            return Response({field: node[field]} if field in node else {"id": path})
        if path == WABA:
            return Response({"id": WABA, "name": NAME, "business_verification_status": "verified"}) if self.waba_ok else Response(NOT_ACCESSIBLE, 400)
        if path == f"{WABA}/phone_numbers":
            return Response({"data": self.rows}) if self.waba_ok else Response(NOT_ACCESSIBLE, 400)
        raise AssertionError(f"unexpected request: {path}")


def run(meta, *, configured=CONFIGURED, target=TARGET, waba=""):
    lines = []
    cfg = module.Config(api_version="v21.0", token=TOKEN, configured_id=configured, target_id=target, waba_id=waba,
                        other_secrets=[APP_SECRET, VERIFY_TOKEN])
    code = module.Probe(cfg, meta, lines.append).run(False)
    output = "\n".join(lines)
    assert_clean(output)
    return code, output


def assert_clean(output):
    for item in SENSITIVE:
        assert item not in output, item
    assert not re.search(r"\d{7,}", output), output


def test_supported_fields_are_reported_with_identifiers_hidden():
    code, out = run(Meta())
    assert code == 0
    assert "graph_api_version: v21.0" in out and "sending_enabled: False" in out
    assert "configured_sender_is_the_investigated_sender: False" in out
    assert f"verified_name: {NAME}" in out and "status: CONNECTED" in out and "quality_rating: GREEN" in out
    assert "name_status: PENDING_REVIEW" in out and "new_name_status: NONE" in out
    assert "id: [hidden]" in out and "can_send_message" in out


def test_the_two_senders_can_be_told_apart():
    _, out = run(Meta())
    assert "sender A (investigated)" in out and "sender B (configured on production)" in out
    assert "display_phone_number: *****82" in out and "display_phone_number: *****80" in out


def test_one_sender_is_inspected_once_when_they_are_the_same():
    meta = Meta()
    _, out = run(meta, configured=TARGET)
    assert "configured_sender_is_the_investigated_sender: True" in out and "sender B" not in out
    assert CONFIGURED not in meta.calls


def test_unsupported_fields_are_recorded_and_the_rest_continue():
    _, out = run(Meta())
    assert "search_visibility: field_unsupported http=400 code=100" in out
    assert "messaging_limit_tier: field_unsupported" in out
    assert "throughput: no value from Meta" in out and "quality_rating: GREEN" in out


def test_missing_name_status_is_not_read_as_approval_or_rejection():
    target = {"id": TARGET, "display_phone_number": TARGET_NUMBER, "verified_name": NAME}
    _, out = run(Meta(target=target))
    assert "name_status: no value from Meta (not evidence of approval or rejection)" in out
    for word in ("APPROVED", "DECLINED"):
        assert word not in out


def test_an_expired_token_stops_everything():
    meta = Meta(target=EXPIRED)
    code, out = run(meta)
    assert code == 2 and "invalid or expired" in out and "done:" not in out
    assert meta.calls == [TARGET]


def test_a_sender_the_token_cannot_reach_is_reported_and_others_continue():
    meta = Meta(target=NOT_ACCESSIBLE)
    code, out = run(meta)
    assert code == 0 and "NOT ACCESSIBLE with this token" in out
    assert meta.calls.count(TARGET) == 1
    assert "verified_name: Test Number" in out


def test_waba_confirmed_only_from_its_own_number_list():
    _, out = run(Meta())
    assert "waba_association: CONFIRMED" in out and "has_investigated=True" in out


def test_waba_mismatch_is_reported():
    rows = [{"id": CONFIGURED, "display_phone_number": CONFIGURED_NUMBER, "verified_name": "Test Number"}]
    _, out = run(Meta(waba_rows=rows), waba=WABA)
    assert "waba_association: MISMATCH" in out and "has_investigated=False has_configured=True" in out


def test_no_waba_is_assumed_when_scopes_list_no_targets():
    debug = {"data": {"is_valid": True, "type": "SYSTEM_USER", "granular_scopes": [{"scope": "whatsapp_business_management"}]}}
    meta = Meta(debug=debug)
    _, out = run(meta)
    assert "waba_association: NOT DETERMINED" in out and "needed from WhatsApp Manager" in out
    assert not any("phone_numbers" in c for c in meta.calls)


def test_a_failed_token_lookup_is_not_a_conclusion():
    debug = {"error": {"code": 100, "message": f"Unsupported request {WABA}"}}
    _, out = run(Meta(debug=debug))
    assert "token lookup: " in out and "waba_association: NOT DETERMINED" in out


def test_an_unreadable_waba_is_not_a_mismatch():
    _, out = run(Meta(waba_ok=False), waba=WABA)
    assert "waba_association: NOT DETERMINED" in out and "MISMATCH" not in out


def test_the_waba_secret_is_used_without_a_token_lookup():
    meta = Meta()
    run(meta, waba=WABA)
    assert "debug_token" not in meta.calls


@pytest.mark.parametrize("message", [
    f"Object {TARGET} of WABA {WABA} for {TARGET_NUMBER} failed",
    f"https://graph.facebook.com/v21.0/{TARGET}?access_token={TOKEN}",
    "Bad value wamid.HBgMOTE5MDAwMDAwMDAxFQ and +919000000001 and 91 90000 00001",
    f"secret {APP_SECRET} verify {VERIFY_TOKEN} other EAAOtherTokenValue1234567",
])
def test_meta_errors_with_sensitive_identifiers_are_cleaned(message):
    target = {"id": TARGET, "error": {"code": 131000, "message": message}}
    code, out = run(Meta(target=target))
    assert code == 0 and "meta_error" in out
    assert "+919000000001" not in out and "EAAOther" not in out and "HBgM" not in out


def test_a_network_error_prints_only_its_type():
    def broken(url, **kwargs):
        raise ConnectionError(f"{url}?input_token={TOKEN}")

    code, out = run(broken, waba=WABA)
    assert code == 0 and "request failed: ConnectionError" in out


def test_an_unset_target_stops():
    code, out = run(Meta(), target="")
    assert code == 1 and "STOP" in out


def test_mask_number():
    assert module.mask_number(TARGET_NUMBER) == "*****82"
    assert module.mask_number("") == "(none)"
    assert module.mask_number("12") == "(none)"


def test_the_script_can_only_make_get_requests():
    tree = ast.parse(SCRIPT.read_text(encoding="utf8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not names & {"post", "put", "patch", "delete", "request", "Session"}
    used = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "requests"]
    assert [n.attr for n in used] == ["get"]


def test_every_probe_request_went_through_the_one_get():
    meta = Meta()
    run(meta)
    assert meta.calls and meta.calls[0] == TARGET and f"{WABA}/phone_numbers" in meta.calls


def _workflow(name):
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf8"))


def test_the_workflow_takes_ids_from_secrets_and_has_no_inputs():
    text = (WORKFLOWS / "probe-whatsapp-identity.yml").read_text(encoding="utf8")
    trigger = _workflow("probe-whatsapp-identity.yml").get(True) or _workflow("probe-whatsapp-identity.yml")["on"]
    assert trigger == {"workflow_dispatch": None}
    assert "inputs." not in text and "${{ secrets.WHATSAPP_DIAGNOSTIC_PHONE_NUMBER_ID }}" in text
    assert "${{ secrets.WHATSAPP_DIAGNOSTIC_WABA_ID }}" in text
    assert not re.search(r"\d{7,}", text)
    assert "set -x" not in text and "echo \"$TARGET_ID" not in text


@pytest.mark.parametrize("name", ["probe-whatsapp-inbound.yml", "apply-whatsapp-config.yml"])
def test_other_workflows_print_no_full_sender_number(name):
    text = (WORKFLOWS / name).read_text(encoding="utf8")
    assert 'data.get("display_phone_number")' in text  # still read, to mask it
    for line in text.splitlines():
        if "print(" in line or "configured_sender" in line:
            if 'data.get("display_phone_number")' in line:
                assert "mask_sender(" in line, line
    assert "def mask_sender(" in text  # defined, not only called


@pytest.mark.parametrize("name", ["probe-whatsapp-inbound.yml", "apply-whatsapp-config.yml"])
def test_the_masking_helper_works_and_the_embedded_python_compiles(name):
    workflow = _workflow(name)
    sources = []
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            for chunk in str(step.get("run", "")).split("<<'PY'\n")[1:]:
                sources.append(chunk.split("\nPY", 1)[0])
    assert sources
    for source in sources:
        compile(source, name, "exec")
    source = next(s for s in sources if "def mask_sender(" in s)
    start = source.index("def mask_sender(")
    end = source.index("\n\n", start)
    scope = {}
    exec(source[start:end], scope)  # noqa: S102 - our own helper, read from our own workflow
    assert scope["mask_sender"](TARGET_NUMBER) == "*****82"
    assert TARGET_NUMBER not in scope["mask_sender"](TARGET_NUMBER)
