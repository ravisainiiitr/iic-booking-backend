"""The Firebase service-account key for Android call pushes: from the environment (base64 or JSON) or a file."""

from __future__ import annotations

import base64
import json

from iic_booking.calls import push

ACCOUNT = {
    "type": "service_account",
    "project_id": "iic-booking-test",
    "private_key_id": "abc",
    "private_key": "-----BEGIN PRIVATE KEY-----\nMIIfake\n-----END PRIVATE KEY-----\n",
    "client_email": "fcm@iic-booking-test.iam.gserviceaccount.com",
    "token_uri": "https://oauth2.googleapis.com/token",
}


def test_base64_and_plain_json_are_both_read():
    raw = json.dumps(ACCOUNT)
    assert push.parse_service_account(raw)["project_id"] == "iic-booking-test"
    assert push.parse_service_account(base64.b64encode(raw.encode()).decode())["client_email"] == ACCOUNT["client_email"]


def test_anything_else_is_not_a_key():
    assert push.parse_service_account("") is None
    assert push.parse_service_account("not base64 !!") is None
    assert push.parse_service_account(json.dumps({**ACCOUNT, "type": "authorized_user"})) is None
    assert push.parse_service_account(json.dumps({k: v for k, v in ACCOUNT.items() if k != "private_key"})) is None
    # google-services.json (the app's file) is not the server key
    assert push.parse_service_account(json.dumps({"project_info": {"project_id": "x"}, "client": []})) is None


def test_environment_wins_over_the_file(settings, tmp_path):
    other = tmp_path / "key.json"
    other.write_text(json.dumps({**ACCOUNT, "project_id": "from-file"}), encoding="utf-8")
    settings.CALLS_FCM_SERVICE_ACCOUNT_FILE = str(other)
    settings.CALLS_FCM_SERVICE_ACCOUNT_JSON = ""
    assert push._fcm_account()["project_id"] == "from-file" and push.fcm_configured()
    settings.CALLS_FCM_SERVICE_ACCOUNT_JSON = base64.b64encode(json.dumps(ACCOUNT).encode()).decode()
    assert push._fcm_account()["project_id"] == "iic-booking-test"


def test_not_configured_without_either(settings):
    settings.CALLS_FCM_SERVICE_ACCOUNT_JSON = ""
    settings.CALLS_FCM_SERVICE_ACCOUNT_FILE = ""
    assert push.fcm_configured() is False
