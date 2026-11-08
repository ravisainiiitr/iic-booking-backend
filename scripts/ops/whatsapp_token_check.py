"""READ-ONLY check of the WhatsApp access token in the repository secret (probe-whatsapp-identity.yml on its branch).

GET requests only: sends no message and creates, changes or deletes nothing. The run log is public, so it prints no
token, app id, WABA id, Phone Number ID or full phone number. Numbers are told apart by their last two digits.
Uses only the standard library so it runs on the runner host.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://graph.facebook.com/v21.0"
SCOPES = ("whatsapp_business_management", "whatsapp_business_messaging")
TOKEN = os.environ.get("NEW_TOKEN", "").strip()
WABA_ID = os.environ.get("WABA_ID", "").strip()
PHONE_ID = os.environ.get("PHONE_ID", "").strip()
DIAG_ID = os.environ.get("DIAG_ID", "").strip()
SECRETS = sorted((s for s in (TOKEN, WABA_ID, PHONE_ID, DIAG_ID) if s), key=len, reverse=True)
results: list[tuple[str, str, str]] = []


def clean(value) -> str:
    text = json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else str(value)
    for secret in SECRETS:
        text = text.replace(secret, "[hidden]")
    text = re.sub(r"https?://\S+", "[url]", text)
    text = re.sub(r"\bEA[A-Za-z0-9]{10,}\b", "[token]", text)
    return re.sub(r"\+?\d[\d\s().-]{5,}\d", "[number]", text)[:300]


def tail(number) -> str:
    digits = re.sub(r"\D", "", str(number or ""))
    return f"ending {digits[-2:]}" if digits else "no number"


def get(path, params=None):
    """(data, error_text). The URL, which can hold the token, is never printed."""
    query = urllib.parse.urlencode(params or {})
    request = urllib.request.Request(f"{API}/{path}" + (f"?{query}" if query else ""),
                                     headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read() or b"{}"), ""
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read() or b"{}").get("error", {})
        except ValueError:
            err = {}
        return None, clean(f"http={exc.code} code={err.get('code')} subcode={err.get('error_subcode')} "
                           f"type={err.get('type')} message={err.get('error_user_msg') or err.get('message')}")
    except Exception as exc:  # noqa: BLE001 - the text of a network error can hold the URL
        return None, f"network error: {type(exc).__name__}"


def record(name, ok, detail):
    results.append((name, "PASS" if ok else "FAIL", detail))


def main():
    missing = [n for n, v in (("WHATSAPP_ACCESS_TOKEN", TOKEN), ("WHATSAPP_WABA_ID", WABA_ID),
                              ("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)) if not v]
    if missing:
        raise SystemExit("secrets not set: " + ", ".join(missing))
    print(f"Configured Phone Number ID is the institutional sender under investigation: {PHONE_ID == DIAG_ID}")

    data, err = get("debug_token", {"input_token": TOKEN})
    info = (data or {}).get("data", {})
    scopes = info.get("scopes", []) or []
    record("1 Token valid", bool(info.get("is_valid")), err or clean(
        f"type={info.get('type')} app={info.get('application')} expires_at={info.get('expires_at')} "
        f"data_access_expires_at={info.get('data_access_expires_at')} error={info.get('error')}"))
    for scope in SCOPES:
        grant = next((g for g in info.get("granular_scopes", []) or [] if g.get("scope") == scope), None)
        targets = (grant or {}).get("target_ids")
        covers = "all assets" if grant is not None and not targets else (
            f"institutional WABA in targets: {WABA_ID in targets}" if targets else "no grant")
        record(f"2 Scope {scope}", scope in scopes and (not targets or WABA_ID in targets), err or covers)

    data, err = get(WABA_ID, {"fields": "id,name,account_review_status,business_verification_status,ownership_type"})
    name = (data or {}).get("name", "")
    record("3 WABA access", data is not None and data.get("id") == WABA_ID, err or clean(
        f"name={name!r} review={data.get('account_review_status')} business_verification="
        f"{data.get('business_verification_status')} ownership={data.get('ownership_type')}"))
    record("4 WABA is institutional, not Test Messaging Account",
           data is not None and "test" not in name.lower(), err or clean(f"name={name!r}"))

    data, err = get(f"{WABA_ID}/phone_numbers",
                    {"fields": "id,display_phone_number,verified_name,status,quality_rating,code_verification_status"})
    numbers = (data or {}).get("data", []) or []
    lines = [clean(f"{tail(n.get('display_phone_number'))} verified_name={n.get('verified_name')!r} "
                   f"status={n.get('status')} quality={n.get('quality_rating')} "
                   f"is_configured={n.get('id') == PHONE_ID}") for n in numbers]
    record("5 WABA phone numbers", data is not None and bool(numbers), err or f"{len(numbers)} found: " + "; ".join(lines))
    record("6 Configured number belongs to this WABA", any(n.get("id") == PHONE_ID for n in numbers),
           err or "listed" if any(n.get("id") == PHONE_ID for n in numbers) else (err or "NOT in this WABA's list"))

    data, err = get(PHONE_ID, {"fields": "id,display_phone_number,verified_name,name_status,status,quality_rating,"
                                         "code_verification_status,messaging_limit_tier,platform_type"})
    record("7 Configured Phone Number ID access", data is not None and data.get("id") == PHONE_ID, err or clean(
        f"{tail(data.get('display_phone_number'))} verified_name={data.get('verified_name')!r} "
        f"name_status={data.get('name_status')} status={data.get('status')} quality={data.get('quality_rating')} "
        f"tier={data.get('messaging_limit_tier')} platform={data.get('platform_type')}"))

    data, err = get(f"{WABA_ID}/message_templates", {"fields": "name,status,category,language", "limit": 100})
    templates = (data or {}).get("data", []) or []
    record("8 Message templates", data is not None, err or f"{len(templates)} at Meta")
    for t in sorted(templates, key=lambda t: t.get("name", "")):
        print(clean(f"  template {t.get('name')} [{t.get('language')}] {t.get('category')} {t.get('status')}"))

    print()
    for name, verdict, detail in results:
        print(f"{verdict}  {name}: {detail}")


if __name__ == "__main__":
    main()
