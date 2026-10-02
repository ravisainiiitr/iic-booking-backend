"""
Read-only production smoke test of Booking Assistant day-to-day answers, as test.student@iic-booking.test.

Sends a few everyday questions through the same endpoint the chat panel uses and prints, per question, the
latency, which layer answered, whether a bookings / wallet card came back, how many follow-up buttons there
are and whether the reply contains raw "seed://" links or the old generic "To book equipment" paragraph.
Reply text, balances and booking details are never printed. Nothing is booked, cancelled or paid: the
questions are reads only, and the conversation the script creates for the test account is deleted at the end.

Usage (inside the django container): python - < scripts/ops/smoke_booking_assistant_daily.py
"""

import os
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from iic_booking.research_copilot.models import Conversation  # noqa: E402

STUDENT_EMAIL = "test.student@iic-booking.test"
BASE = "/api/v1/research-copilot"
QUESTIONS = [
    "List my recent bookings.",
    "wallet balance",
    "how to recharge wallet",
    "Show my upcoming bookings",
    "help",
]
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append(bool(ok))
    print("PASS" if ok else "FAIL", "|", name, "|", detail)


def _host():
    hosts = [h for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*" and not h.startswith(".")]
    return hosts[0] if hosts else "localhost"


def main():
    user = get_user_model().objects.filter(email__iexact=STUDENT_EMAIL, is_active=True).first()
    if user is None:
        print("STOP | test student account not found or inactive")
        return 1
    client = APIClient(HTTP_HOST=_host())
    client.force_authenticate(user=user)
    created = client.post(f"{BASE}/conversations/", {}, format="json")
    if created.status_code not in (200, 201):
        print("STOP | could not open a conversation | status", created.status_code)
        return 1
    conv_id = created.json()["conversation"]["id"]
    try:
        for q in QUESTIONS:
            started = time.perf_counter()
            res = client.post(f"{BASE}/conversations/{conv_id}/messages/", {"content": q}, format="json")
            ms = int((time.perf_counter() - started) * 1000)
            if res.status_code != 200:
                check(q, False, f"status={res.status_code} ms={ms}")
                continue
            body = res.json()
            msg = body.get("message") or {}
            meta = msg.get("metadata") or {}
            content = msg.get("content") or ""
            cards = [c.get("type") for c in (meta.get("cards") or body.get("cards") or [])]
            cites = msg.get("citations") or []
            seed = "seed://" in content or any("seed://" in str(c.get("url") or "") for c in cites)
            generic = "To book equipment: open **Equipments**" in content
            print(
                "INFO |", q, "|",
                f"ms={ms}",
                f"intent={meta.get('daily_intent') or meta.get('intent') or ''}",
                f"llm_used={bool(meta.get('llm_used'))}",
                f"cards={','.join(str(c) for c in cards) or '-'}",
                f"actions={len(msg.get('suggested_actions') or [])}",
                f"citations={len(cites)}",
                f"seed_link={seed}",
                f"generic_reply={generic}",
            )
            check(f"{q} | no raw seed:// links", not seed)
            check(f"{q} | not the generic booking paragraph", not generic)
            check(f"{q} | answered in under 1.5 s", ms < 1500, f"ms={ms}")
    finally:
        try:
            Conversation.objects.filter(pk=conv_id, user=user).delete()
        except Exception as exc:  # noqa: BLE001
            print("WARN | test conversation not deleted |", type(exc).__name__)
    passed = sum(RESULTS)
    print(f"SUMMARY | {passed}/{len(RESULTS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
