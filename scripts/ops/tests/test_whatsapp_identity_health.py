"""health_status parsing and WABA detection in the WhatsApp identity probe, against a fake Meta API. No Django or network.

    python -m pytest scripts/ops/tests -p no:django -o addopts="" --import-mode=importlib
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts/ops/whatsapp_identity_probe.py"
spec = importlib.util.spec_from_file_location("whatsapp_identity_probe_health", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

TOKEN = "EAAFakeTokenValueNotReal123456789"
TARGET, CONFIGURED = "1000000000000111", "2000000000000222"
WABA, OTHER_WABA, BUSINESS, APP = "3000000000000333", "3500000000000355", "4000000000000444", "5000000000000555"
TARGET_NUMBER, CONFIGURED_NUMBER = "+1 555-010-0182", "+1 555-010-0180"
SENSITIVE = (TOKEN, TARGET, CONFIGURED, WABA, OTHER_WABA, BUSINESS, APP, TARGET_NUMBER, CONFIGURED_NUMBER, "555-010")
NAME = "Indian Institute of Technology Roorkee"
NOT_READABLE = {"error": {"code": 100, "error_subcode": 33, "message": "Object does not exist, cannot be loaded due to missing permissions"}}
NO_PERMISSION = {"error": {"code": 200, "message": "Permissions error"}}
LONG_INFO = "Your display name has not been approved yet. " + "This sentence pads the reply beyond any old limit. " * 12 + "THE-END"


def health(phone_id=TARGET, waba=WABA, *, overall="BLOCKED", phone_send="LIMITED", extra=None):
    return {
        "can_send_message": overall,
        "entities": [
            {"entity_type": "PHONE_NUMBER", "id": phone_id, "can_send_message": phone_send, "can_receive_call_sip": "BLOCKED",
             "additional_info": [LONG_INFO],
             "errors": [{"error_code": 138024, "error_description": "WhatsApp Business calling cannot use SIP because it is not enabled",
                         "possible_solution": "Configure SIP using {PHONE_NUMBER_ID}/settings API"}]},
            {"entity_type": "WABA", "id": waba, "can_send_message": "AVAILABLE"},
            {"entity_type": "BUSINESS", "id": BUSINESS, "can_send_message": "AVAILABLE"},
            {"entity_type": "APP", "id": APP, "can_send_message": "AVAILABLE", "can_receive_call_sip": "BLOCKED", **(extra or {})},
        ],
    }


class Response:
    def __init__(self, body, status_code=200):
        self.body, self.status_code = body, status_code

    def json(self):
        return self.body


class Meta:
    def __init__(self, nodes=None, lists=None, business=None):
        self.requests = []
        self.nodes = {TARGET: {"display_phone_number": TARGET_NUMBER, "verified_name": NAME, "health_status": health()},
                      CONFIGURED: {"display_phone_number": CONFIGURED_NUMBER, "verified_name": "Test Number", "health_status": health(CONFIGURED)},
                      WABA: {"name": NAME, "business_verification_status": "verified"}}
        self.nodes.update(nodes or {})
        self.lists = {WABA: [{"id": TARGET, "display_phone_number": TARGET_NUMBER}, {"id": CONFIGURED, "display_phone_number": CONFIGURED_NUMBER}]} if lists is None else lists
        self.business = business if business is not None else NO_PERMISSION

    def __call__(self, url, params=None, headers=None, timeout=None):
        path, fields = url.split("/v21.0/", 1)[1], (params or {}).get("fields", "")
        self.requests.append((path, fields))
        if path == "debug_token":
            return Response({"data": {"is_valid": True, "type": "SYSTEM_USER", "granular_scopes": [{"scope": "whatsapp_business_management"}]}})
        if path.endswith("/message_templates"):
            return Response({"data": []})
        if path.endswith("_whatsapp_business_accounts"):
            return Response(self.business, 400 if "error" in self.business else 200)
        if path.endswith("/phone_numbers"):
            waba = path.split("/")[0]
            return Response({"data": self.lists[waba]}) if waba in self.lists else Response(NOT_READABLE, 400)
        node = self.nodes.get(path)
        if node is None or "error" in (node or {}):
            return Response(node or NOT_READABLE, 400)
        if "," in fields:
            return Response({k: v for k, v in node.items() if k in fields.split(",")})
        return Response({fields: node[fields]} if fields in node else {"id": path})


def run(meta, *, waba=""):
    lines = []
    cfg = module.Config(api_version="v21.0", token=TOKEN, configured_id=CONFIGURED, target_id=TARGET, waba_id=waba)
    code = module.Probe(cfg, meta, lines.append).run(False)
    output = "\n".join(lines)
    for item in SENSITIVE:
        assert item not in output, item
    assert not re.search(r"\d{7,}", output), output
    return code, output


def test_the_whole_health_report_is_printed_untruncated():
    _, out = run(Meta())
    assert LONG_INFO in out and len(LONG_INFO) > 500


def test_every_health_field_is_reported():
    _, out = run(Meta())
    assert "health_status.can_send_message: BLOCKED" in out
    assert "health_status.entities[0].entity_type: PHONE_NUMBER" in out
    assert "health_status.entities[0].can_send_message: LIMITED" in out
    assert "health_status.entities[0].can_receive_call_sip: BLOCKED" in out
    assert "health_status.entities[0].additional_info[0]: Your display name has not been approved yet." in out
    assert "health_status.entities[0].errors[0].error_code: 138024" in out
    assert "health_status.entities[0].errors[0].error_description: WhatsApp Business calling cannot use SIP" in out
    assert "{PHONE_NUMBER_ID}/settings API" in out
    assert "health_status.entities[3].entity_type: APP" in out


def test_fields_meta_adds_later_are_not_lost():
    meta = Meta(nodes={TARGET: {"health_status": health(extra={"new_field": {"nested": ["value one"]}})}})
    _, out = run(meta)
    assert "new_field.nested[0]: value one" in out


def test_summary_separates_messaging_from_sip():
    _, out = run(Meta())
    assert "overall=BLOCKED entities_blocking_messaging=none entities_limiting_messaging=['PHONE_NUMBER']" in out
    assert "entities_blocking_sip_calls=['PHONE_NUMBER', 'APP']" in out
    assert "error_codes=['138024 on PHONE_NUMBER']" in out
    assert "health note: overall is BLOCKED, but no entity reports can_send_message=BLOCKED" in out


def test_no_note_when_an_entity_really_blocks_messaging():
    meta = Meta(nodes={TARGET: {"health_status": health(phone_send="BLOCKED")}})
    _, out = run(meta)
    assert "entities_blocking_messaging=['PHONE_NUMBER']" in out
    lines = out.split("--- sender B")[0]
    assert "health note" not in lines


def test_entity_ids_are_hidden_on_every_entity():
    _, out = run(Meta())
    assert "health_status.entities[1].id: [hidden]" in out and "health_status.entities[2].id: [hidden]" in out


@pytest.mark.parametrize("text", [
    f"Contact {TARGET_NUMBER} about {WABA}",
    f"See https://business.facebook.com/wa/{BUSINESS}?token={TOKEN}",
    "Sender +919000000001 or 91 90000 00001",
    "Message wamid.HBgMOTE5MDAwMDAwMDAxFQ failed",
])
def test_sensitive_text_inside_health_fields_is_hidden(text):
    report = health()
    report["entities"][0]["additional_info"] = [text]
    report["entities"][0]["errors"][0]["possible_solution"] = text
    _, out = run(Meta(nodes={TARGET: {"health_status": report}}))
    assert "+919000000001" not in out and "HBgM" not in out and "business.facebook" not in out


def test_dates_and_short_error_codes_stay_readable():
    report = health()
    report["last_seen"] = "2026-11-07"
    _, out = run(Meta(nodes={TARGET: {"health_status": report, "last_onboarded_time": "2026-11-06T10:00:00+0000"}}))
    assert "last_seen: 2026-11-07" in out and "last_onboarded_time: 2026-11-06T" in out and "error_code: 138024" in out


def test_a_long_code_looking_value_is_hidden_not_printed():
    report = health()
    report["entities"][0]["errors"][0]["error_code"] = int(WABA)
    run(Meta(nodes={TARGET: {"health_status": report}}))


def test_the_waba_named_by_health_is_only_a_candidate_until_its_list_confirms_it():
    meta = Meta()
    _, out = run(meta)
    assert "candidates: 1 (health_status)" in out and "waba_association: CONFIRMED" in out
    assert (f"{WABA}/phone_numbers", module.LIST_FIELDS) in meta.requests
    assert "name_matches_institute: True" in out


def test_a_waba_named_by_health_whose_list_lacks_the_sender_is_a_mismatch():
    _, out = run(Meta(lists={WABA: [{"id": CONFIGURED, "display_phone_number": CONFIGURED_NUMBER}]}))
    assert "waba_association: MISMATCH" in out and "waba_association_configured: CONFIRMED" in out


def test_a_waba_named_by_health_with_an_unreadable_list_is_not_confirmed():
    meta = Meta(lists={})
    _, out = run(meta)
    assert "waba_association: NOT CONFIRMED" in out and "CONFIRMED: listed" not in out
    assert "needed from WhatsApp Manager" in out


def test_reading_a_number_with_the_token_is_not_association():
    plain = {"display_phone_number": TARGET_NUMBER, "verified_name": NAME}
    meta = Meta(nodes={TARGET: plain, CONFIGURED: {"verified_name": "Test Number"}})
    _, out = run(meta)
    assert "waba_association: NOT DETERMINED" in out


def test_a_waba_found_through_a_business_list_still_needs_its_own_list():
    meta = Meta(nodes={TARGET: {"health_status": {"entities": [{"entity_type": "BUSINESS", "id": BUSINESS}]}},
                       CONFIGURED: {"verified_name": "Test Number"}, OTHER_WABA: {"name": "Other"}},
                lists={OTHER_WABA: [{"id": TARGET, "display_phone_number": TARGET_NUMBER}]},
                business={"data": [{"id": OTHER_WABA}]})
    _, out = run(meta)
    assert "business lists" in out and "owned_whatsapp_business_accounts: 1 WABA(s)" in out
    assert "waba_association: CONFIRMED" in out and "name_matches_institute: False" in out


def test_business_list_errors_are_recorded_and_do_not_conclude_anything():
    plain = {"health_status": {"entities": [{"entity_type": "BUSINESS", "id": BUSINESS}]}}
    _, out = run(Meta(nodes={TARGET: plain, CONFIGURED: {"verified_name": "Test Number"}}))
    assert "owned_whatsapp_business_accounts: permission_denied" in out and "waba_association: NOT DETERMINED" in out


def test_senders_sharing_a_waba_are_reported_only_from_lists():
    _, out = run(Meta())
    assert "senders_share_a_waba: True" in out
    second = {OTHER_WABA: {"name": "Other"}, CONFIGURED: {"health_status": health(CONFIGURED, OTHER_WABA)}}
    lists = {WABA: [{"id": TARGET}], OTHER_WABA: [{"id": CONFIGURED}]}
    _, out = run(Meta(nodes=second, lists=lists))
    assert "senders_share_a_waba: False" in out
    _, out = run(Meta(lists={}))
    assert "senders_share_a_waba: unknown" in out


def test_the_waba_secret_skips_other_lookups_but_still_needs_the_list():
    meta = Meta(nodes={TARGET: {"verified_name": NAME}, CONFIGURED: {"verified_name": "Test Number"}}, lists={WABA: [{"id": TARGET}]})
    _, out = run(meta, waba=WABA)
    assert "debug_token" not in [p for p, _ in meta.requests] and "candidates: 1 (secret)" in out
    assert "waba_association: CONFIRMED" in out


def test_templates_are_listed_across_pages_with_their_wording():
    class Paged(Meta):
        def __call__(self, url, params=None, headers=None, timeout=None):
            path = url.split("/v21.0/", 1)[1]
            if path.endswith("/message_templates"):
                self.requests.append((path, params.get("after", "")))
                if not params.get("after"):
                    return Response({"data": [{"id": WABA, "name": "hello_world", "language": "en_US", "status": "APPROVED",
                                               "category": "UTILITY", "components": [{"type": "BODY", "text": "Booking {{1}} is ready"}, {"type": "BUTTONS"}]}],
                                     "paging": {"cursors": {"after": "CURSOR"}, "next": "https://graph/next"}})
                return Response({"data": [{"name": "second_one", "language": "en", "status": "REJECTED", "category": "UTILITY",
                                           "rejected_reason": f"INVALID_FORMAT {TARGET_NUMBER}"}], "paging": {"cursors": {"after": "END"}}})
            return super().__call__(url, params=params, headers=headers, timeout=timeout)

    _, out = run(Paged())
    assert "templates: 2" in out and "name=hello_world language=en_US status=APPROVED category=UTILITY" in out
    assert "name=second_one language=en status=REJECTED" in out and "rejected_reason=INVALID_FORMAT" in out
    assert "BODY: Booking {{1}} is ready" in out


def test_a_template_list_error_does_not_stop_the_waba_check():
    class Refused(Meta):
        def __call__(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/message_templates"):
                return Response(NO_PERMISSION, 403)
            return super().__call__(url, params=params, headers=headers, timeout=timeout)

    _, out = run(Refused())
    assert "templates: permission_denied" in out and "waba_association: CONFIRMED" in out


def test_no_certificate_or_credential_field_is_ever_requested():
    meta = Meta()
    run(meta)
    requested = ",".join(f for _, f in meta.requests).lower()
    for field in ("certificate", "access_token", "pin", "webhook"):
        assert not re.search(rf"(^|,){field}(,|$)", requested), field
    assert "certificate" not in requested
