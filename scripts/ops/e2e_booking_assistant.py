"""
End-to-end check of Booking Assistant in-chat booking on production, as test.student@iic-booking.test.

Drives the real Copilot endpoints the chat panel uses: ask for availability, tap a slot chip, fill the
booking form, review the summary, then press Confirm (POST /mutations/confirm/). Verifies that the
booking went through the normal booking service and that a second confirm with the same token cannot
create another booking. The booking is then cancelled straight away through the normal My Bookings
cancel endpoint with refund, and the script checks the slot is free again and the wallet is back to its
starting balance. A slot outside the equipment's cancellation window is chosen so the user cancel is
allowed; if none exists the script stops before booking. Emails go to the in-memory backend and Celery
tasks are not queued, so OICs and supervisors are not notified about the test booking.

Usage (inside the django container): python - < scripts/ops/e2e_booking_assistant.py
E2E_MODE=readonly stops after the summary card (nothing is booked).
"""

import os
import sys
import uuid

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from datetime import timedelta  # noqa: E402
from decimal import Decimal  # noqa: E402
from unittest.mock import patch  # noqa: E402

from celery.app.task import Task  # noqa: E402
from django.conf import settings  # noqa: E402
from django.contrib.auth import get_user_model  # noqa: E402
from django.db.models import Max, Q  # noqa: E402
from django.test.utils import override_settings  # noqa: E402
from django.utils import timezone  # noqa: E402
from django.utils.dateparse import parse_datetime  # noqa: E402
from rest_framework.test import APIClient  # noqa: E402

from iic_booking.equipment.api_views import get_visible_equipment_queryset  # noqa: E402
from iic_booking.equipment.models import Booking, BookingStatus, DailySlot, SlotStatus  # noqa: E402
from iic_booking.research_copilot.services.assistant import booking_flow  # noqa: E402
from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed  # noqa: E402
from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots  # noqa: E402
from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction  # noqa: E402

STUDENT_EMAIL = "test.student@iic-booking.test"
BASE = "/api/v1/research-copilot"
# Low-contention instruments the earlier Copilot booking E2E used; anything else visible comes after.
PREFERRED = [43, 47, 44, 60, 7, 67, 1, 66, 4]
MODE = (os.environ.get("E2E_MODE") or "full").strip().lower()
STARTED = timezone.now()
RESULTS = []
ACTIVE = (BookingStatus.BOOKED, BookingStatus.PENDING)


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print("PASS" if ok else "FAIL", "|", name, "|", detail)
    return bool(ok)


def _host():
    hosts = [h for h in getattr(settings, "ALLOWED_HOSTS", []) if h and h != "*" and not h.startswith(".")]
    return hosts[0] if hosts else "localhost"


def _client(user):
    client = APIClient(HTTP_HOST=_host())
    client.force_authenticate(user=user)
    return client


def _data(res):
    data = getattr(res, "data", None)
    if data is None:
        try:
            data = res.json()
        except Exception:  # noqa: BLE001
            data = {"raw": res.content[:300]}
    return data


def _send(client, conv, content, action=None):
    body = {"content": content}
    if action:
        body["action"] = action
    res = client.post(f"{BASE}/conversations/{conv}/messages/", body, format="json", secure=True)
    data = _data(res)
    msg = data.get("message") or {}
    cards = (msg.get("metadata") or {}).get("cards") or data.get("cards") or []
    return res.status_code, msg, cards


def _card(cards, kind):
    return next((c for c in cards if c.get("type") == kind), None)


def _types(cards):
    return [c.get("type") for c in cards]


def _confirm(client, action, key):
    res = client.post(
        f"{BASE}/mutations/confirm/",
        {
            "proposal_id": action["proposal_id"],
            "confirmation_token": action["confirmation_token"],
            "action": action.get("mutation_action") or "CREATE_BOOKING",
            "idempotency_key": key,
        },
        format="json",
        secure=True,
    )
    return res.status_code, _data(res)


def _pick_target(student):
    """Visible, chat-bookable equipment with a free slot the user may still cancel themselves."""
    now = timezone.now()
    visible = list(get_visible_equipment_queryset(student).filter(status="ACTIVE").order_by("pk")[:300])
    by_pk = {int(e.pk): e for e in visible}
    ordered = [by_pk[p] for p in PREFERRED if p in by_pk] + [e for e in visible if int(e.pk) not in PREFERRED]
    for eq in ordered[:60]:
        if booking_flow.is_complex(student, eq)[0]:
            continue
        threshold = int(getattr(eq, "reschedule_hours_threshold", None) or 48)
        earliest = now + timedelta(hours=max(threshold + 12, 72))
        start = timezone.localtime(earliest).date()
        lookup = find_bookable_slots(user=student, equipment_id=int(eq.pk), start_date=start,
                                     end_date=start + timedelta(days=13), limit=200)
        rows = [r for r in (lookup.rows or []) if (parse_datetime(r["start"]) or now) >= earliest]
        if rows:
            return eq, rows[0], earliest
    return None, None, None


def _fill_inputs(fields):
    values = {}
    for f in fields or []:
        if not f.get("required"):
            continue
        key, ftype, opts = f["key"], str(f.get("type") or ""), f.get("options") or []
        if f.get("default") not in (None, ""):
            values[key] = str(f["default"])
        elif opts:
            values[key] = str(opts[0].get("value"))
        elif ftype == "NUMERIC":
            values[key] = str(int(f.get("min") or 1))
        elif ftype == "TOGGLE":
            values[key] = "No"
        elif ftype == "PERIODIC_TABLE":
            values[f"{key}_elements"] = str((f.get("allowed") or ["C"])[0])
        else:
            values[key] = "Booking Assistant E2E test"
    return values


def _student_txns(student, after_id):
    return SubWalletTransaction.objects.filter(id__gt=after_id).filter(
        Q(related_user=student) | Q(sub_wallet__wallet__user=student)
    )


def run():
    User = get_user_model()
    student = User.objects.filter(email__iexact=STUDENT_EMAIL).first()
    if student is None:
        check("test student exists", False, "not found; stopping")
        return
    if not check("account is a test account", bool(getattr(student, "is_test_account", False)), f"user {student.pk}"):
        return
    print("mode", MODE, "student", student.pk, "user_type", student.user_type)
    flags = {k: bool(getattr(settings, k, False)) for k in (
        "COPILOT_BOOKING_CREATE", "COPILOT_BOOKING_E2E_TEST_MODE", "RESEARCH_COPILOT_ACTIONS_ENABLED",
        "BOOKING_ASSISTANT_ENABLED")}
    print("flags", flags, "planner", getattr(settings, "BOOKING_ASSISTANT_LLM_PLANNER", None))
    may_book = booking_mutation_allowed(student, "COPILOT_BOOKING_CREATE")
    if MODE != "readonly" and not check("in-chat booking allowed for the test student", may_book):
        return

    client = _client(student)
    res = client.get(f"{BASE}/bootstrap/", secure=True)
    boot = _data(res)
    check("bootstrap", res.status_code == 200, f"http {res.status_code}")
    check("bootstrap reports booking assistant enabled", (boot.get("booking_assistant") or {}).get("enabled") is True)
    print("bootstrap mutation_flags.booking_create =", (boot.get("mutation_flags") or {}).get("booking_create"))

    eq, row, earliest = _pick_target(student)
    if not check("found chat-bookable equipment with a cancellable free slot", eq is not None):
        return
    target_start = timezone.localtime(parse_datetime(row["start"]))
    print("equipment", eq.pk, eq.name, "| target slot", row["slot_id"], target_start.isoformat(),
          "| cancel threshold h", getattr(eq, "reschedule_hours_threshold", None) or 48)

    res = client.post(f"{BASE}/conversations/", {}, format="json", secure=True)
    conv = (_data(res).get("conversation") or {}).get("id")
    if not check("conversation created", res.status_code in (200, 201) and conv, f"http {res.status_code}"):
        return

    got = (_guided_to_summary if MODE == "guided" else _chat_to_summary)(client, conv, eq, earliest, target_start)
    if got is None:
        return
    slot_ids, summary, confirm = got
    print("summary", {k: summary.get(k) for k in ("equipment_name", "when_label", "sample_count", "total_amount",
                                                  "gst_amount", "wallet_label", "executable", "expires_at")})
    if MODE == "readonly":
        check("readonly: nothing booked", not Booking.objects.filter(user=student, daily_slots__in=slot_ids,
                                                                    status__in=ACTIVE).exists())
        print("summary executable =", summary.get("executable"), "confirm button present =", bool(confirm))
        return
    if not check("summary is executable with exactly one Confirm booking button",
                 summary.get("executable") is True and len(confirm) == 1, f"buttons {len(confirm)}"):
        return
    confirm = confirm[0]

    code, msg, _ = _send(client, conv, "confirm")
    check("typing 'confirm' does not book",
          not Booking.objects.filter(user=student, daily_slots__in=slot_ids, status__in=ACTIVE).exists())
    check("typing 'confirm' points to the Confirm booking button",
          (msg.get("metadata") or {}).get("typed_confirm_blocked") is True,
          f"intent {(msg.get('metadata') or {}).get('intent')} | {str(msg.get('content') or '')[:140]}")

    txn_start = SubWalletTransaction.objects.aggregate(m=Max("id"))["m"] or 0
    created_after = timezone.now()
    first_key = f"ba-e2e-{uuid.uuid4().hex}"
    booking = None
    before = {}
    debit_total = Decimal("0")
    try:
        code, data = _confirm(client, confirm, first_key)
        result = data.get("data") or {}
        ok = check("Confirm booking creates the booking", code == 200 and data.get("ok") is True,
                   f"http {code} error {data.get('error')} | {str(data.get('message') or '')[:160]}")
        booking = (
            Booking.objects.filter(user=student, daily_slots__in=slot_ids, created_at__gte=created_after)
            .order_by("-booking_id").first()
        )
        if not ok and booking is None:
            return
        check("booking row exists for the test student on the chosen slot", booking is not None)
        if booking is None:
            return
        print("BOOKING_CREATED", booking.booking_id, getattr(booking, "virtual_booking_id", ""), booking.status,
              "| response booking id", result.get("real_booking_id") or result.get("booking_id"))
        _check_success_reply(booking, data)
        booked_slots = sorted(booking.daily_slots.values_list("id", flat=True))
        check("booking made through the normal service (status, equipment, slots)",
              booking.status in ACTIVE and int(booking.equipment_id) == int(eq.pk)
              and booked_slots == sorted(slot_ids),
              f"status {booking.status} slots {booked_slots}")
        try:
            from iic_booking.research_copilot.models import CopilotAuditEvent

            audited = CopilotAuditEvent.objects.filter(user=student, created_at__gte=created_after,
                                                       message="create_booking").exists()
            check("Copilot audit event recorded", audited)
        except Exception as exc:  # noqa: BLE001
            check("Copilot audit event recorded", False, type(exc).__name__)

        code2, data2 = _confirm(client, confirm, f"ba-e2e-{uuid.uuid4().hex}")
        check("second Confirm with the same token is rejected", data2.get("ok") is not True,
              f"http {code2} error {data2.get('error')}")
        code3, data3 = _confirm(client, confirm, first_key)
        check("retrying the same click is refused or replayed, never booked again",
              data3.get("ok") is not True or data3.get("idempotent_replay") is True,
              f"http {code3} error {data3.get('error')} replay {data3.get('idempotent_replay')}")
        count = Booking.objects.filter(user=student, daily_slots__in=slot_ids, status__in=ACTIVE).distinct().count()
        check("still exactly one booking on that slot", count == 1, f"{count}")

        debits = list(_student_txns(student, txn_start).filter(transaction_type="debit"))
        debit_total = sum((t.amount for t in debits), Decimal("0"))
        for sub_id in {t.sub_wallet_id for t in debits}:
            sub = SubWallet.objects.get(pk=sub_id)
            moved = sum(
                (t.amount if t.transaction_type == "debit" else -t.amount)
                for t in SubWalletTransaction.objects.filter(sub_wallet_id=sub_id, id__gt=txn_start)
            )
            before[sub_id] = sub.balance + moved
        print("wallet debit at booking", str(debit_total), "on sub-wallets", sorted(before),
              "| summary total", summary.get("total_amount"))
    finally:
        if booking is not None:
            _cancel_and_verify(client, student, booking, slot_ids, txn_start, before, debit_total)


def _check_success_reply(booking, data):
    """The chat must show the virtual booking ID, never the database id, and offer the workspace only when enabled."""
    vid = str(getattr(booking, "virtual_booking_id", "") or "")
    envelope = data.get("response") or {}
    text = " ".join(str(x or "") for x in (data.get("message"), envelope.get("content")))
    card = next((c for c in envelope.get("cards") or [] if c.get("type") == "booking_success"), {})
    if vid:
        check("confirmation message shows the virtual booking ID", vid in text, text[:200])
        check("booking card carries the virtual booking ID", card.get("booking_ref") == vid, str(card.get("booking_ref")))
    check("confirmation message does not show the database id", f"#{booking.booking_id}" not in text, text[:200])
    labels = [a.get("label") for a in envelope.get("actions") or envelope.get("suggested_actions") or []]
    enabled = bool(getattr(booking.equipment, "enable_remote_analysis", False))
    check("Open Analysis Workspace offered only when Remote Analysis is enabled",
          ("Open Analysis Workspace" in labels) == enabled, f"remote analysis {enabled} | actions {labels}")


def _flow(client, conv, label, step, **payload):
    return _send(client, conv, label, {"type": "ba_flow", "payload": {"step": step, **payload}})


def _step_index(card):
    return ((card or {}).get("step") or {}).get("index")


def _cancellable_chip(slots, earliest):
    chips = [c for d in (slots or {}).get("days") or [] for c in d.get("slots") or []]
    chips = [c for c in chips if c.get("start") and parse_datetime(c["start"]) >= earliest]
    chips.sort(key=lambda c: c.get("start"))
    return chips[0] if chips else None


def _guided_to_summary(client, conv, eq, earliest, target_start):
    """Book equipment -> department -> equipment -> inputs -> slot -> summary, with Change slot on the way."""
    dept_id = int(eq.internal_department_id or 0)
    code, msg, cards = _flow(client, conv, "Book equipment", "start")
    depts, equipment = _card(cards, "ba_flow_departments"), _card(cards, "ba_flow_equipment")
    if depts:
        items = depts.get("items") or []
        check("step 1 of 5 asks for the department", _step_index(depts) == 1 and bool(items), [d.get("name") for d in items])
        check("every listed department has bookable equipment", all(int(d.get("count") or 0) > 0 for d in items),
              [(d.get("name"), d.get("count")) for d in items])
        item = next((d for d in items if int(d.get("department_id") or 0) == dept_id), None)
        if not check("the target equipment's department is listed", item is not None, f"department {dept_id}"):
            return None
        code, msg, cards = _flow(client, conv, item["name"], "department", department_id=dept_id)
        equipment = _card(cards, "ba_flow_equipment")
    if not check("step 2 of 5 lists that department's equipment",
                 equipment is not None and _step_index(equipment) == 2 and int(equipment.get("department_id") or 0) == dept_id,
                 f"http {code} cards {_types(cards)} | {str(msg.get('content') or '')[:160]}"):
        return None
    rows = equipment.get("items") or []
    ids = {r["equipment_id"] for r in rows} | {m["equipment_id"] for r in rows for m in r.get("modes") or []}
    check("the target equipment is offered (or the list says there are more)", eq.pk in ids or bool(equipment.get("more")),
          f"{len(rows)} rows, more {equipment.get('more')}")
    labels = [a.get("label") for a in msg.get("suggested_actions") or []]
    check("Back to departments and Cancel offered", {"Back to departments", "Cancel"} <= set(labels), labels)

    code, msg, cards = _flow(client, conv, eq.name, "equipment", equipment_id=eq.pk)
    form = _card(cards, "ba_booking_form")
    if not check("step 3 of 5 asks for samples and inputs before any slot",
                 form is not None and form.get("flow") is True and _step_index(form) == 3 and not form.get("slot_ids"),
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:200]}"):
        return None
    samples = int(((form.get("samples") or {}).get("min") or 1)) if form.get("samples") else 1
    inputs = _fill_inputs(form.get("fields"))
    print("form fields", [(f.get("key"), f.get("type"), f.get("required")) for f in form.get("fields") or []],
          "| samples", samples, "| inputs", inputs, "| instruction", bool(form.get("instruction")))

    code, msg, cards = _flow(client, conv, "Choose a slot", "inputs", equipment_id=eq.pk,
                             number_of_samples=samples, input_values=inputs)
    slots = _card(cards, "ba_slots")
    if not check("step 4 of 5 shows slots sized for the inputs",
                 slots is not None and slots.get("flow") is True and _step_index(slots) == 4,
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:200]}"):
        return None
    print("required minutes", slots.get("required_minutes"), "slots needed", slots.get("slots_needed"))
    day = {"start": target_start.date().isoformat(), "end": (target_start.date() + timedelta(days=6)).isoformat()}
    code, msg, cards = _flow(client, conv, "Later", "slots", equipment_id=eq.pk, when=day)
    chip = _cancellable_chip(_card(cards, "ba_slots"), earliest)
    if not check("a cancellable slot is offered in the guided window", chip is not None,
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:160]}"):
        return None
    print("chip", chip.get("label"), chip.get("date"), "slots", chip.get("slot_ids"))

    code, msg, cards = _flow(client, conv, chip["label"], "slot", equipment_id=eq.pk, slot_ids=chip["slot_ids"])
    summary = _card(cards, "ba_booking_summary")
    if not check("step 5 of 5 shows the booking summary", summary is not None and _step_index(summary) == 5,
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:200]}"):
        return None
    labels = [a.get("label") for a in msg.get("suggested_actions") or []]
    check("summary offers Change slot / Change samples/inputs / Change equipment / Cancel",
          {"Change slot", "Change samples/inputs", "Change equipment", "Cancel"} <= set(labels), labels)

    code, msg, cards = _flow(client, conv, "Change slot", "change_slot", equipment_id=eq.pk, when=day)
    check("Change slot goes back to the slot step", _card(cards, "ba_slots") is not None, _types(cards))
    code, msg, cards = _flow(client, conv, chip["label"], "slot", equipment_id=eq.pk, slot_ids=chip["slot_ids"])
    summary = _card(cards, "ba_booking_summary")
    if not check("picking the slot again keeps the inputs and returns to the summary",
                 summary is not None and int(summary.get("sample_count") or 0) == samples,
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:200]}"):
        return None
    confirm = [a for a in msg.get("suggested_actions") or [] if a.get("confirmation_token")]
    return list(chip["slot_ids"]), summary, confirm


def _chat_to_summary(client, conv, eq, earliest, target_start):
    code, msg, cards = _send(client, conv, f"I need {eq.name} tomorrow — what are my options?")
    check("'tomorrow — what are my options?' answered with live availability or equipment choices",
          code == 200 and bool({"ba_slots", "ba_equipment_options"} & set(_types(cards))),
          f"http {code} cards {_types(cards)} | {str(msg.get('content') or '')[:120]}")

    code, msg, cards = _send(client, conv, f"I need {eq.name} on {target_start:%d %b} — what are my options?")
    options = _card(cards, "ba_equipment_options")
    if options and not _card(cards, "ba_slots"):
        item = next((o for o in options.get("items") or [] if o.get("equipment_id") == eq.pk), None)
        check("equipment offered as a clickable option", item is not None, [o.get("name") for o in options.get("items") or []])
        if item is None:
            return
        payload = {"equipment_id": eq.pk, "intent": options.get("intent") or "availability", "when": options.get("when")}
        code, msg, cards = _send(client, conv, item["name"], {"type": "ba_pick_equipment", "payload": payload})
    slots = _card(cards, "ba_slots")
    if not check("slot chips shown for the requested day", code == 200 and slots and slots.get("equipment_id") == eq.pk,
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:160]}"):
        return
    chips = [c for d in slots.get("days") or [] for c in d.get("slots") or []]
    chips = [c for c in chips if c.get("start") and parse_datetime(c["start"]) >= earliest]
    chips.sort(key=lambda c: (len(c.get("slot_ids") or []), c.get("start")))
    if not check("a cancellable slot chip is offered", bool(chips), f"{len(chips)} chips"):
        return
    chip = chips[0]
    print("chip", chip.get("label"), chip.get("date"), "slots", chip.get("slot_ids"), "estimate", slots.get("estimate"))

    code, msg, cards = _send(client, conv, chip["label"],
                             {"type": "ba_pick_slot", "payload": {"equipment_id": eq.pk, "slot_ids": chip["slot_ids"]}})
    form = _card(cards, "ba_booking_form")
    if not check("tapping the chip opens the booking form", form is not None,
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:160]}"):
        return
    samples = ((form.get("samples") or {}).get("min") or 1) if form.get("samples") else None
    inputs = _fill_inputs(form.get("fields"))
    payload = {"equipment_id": eq.pk, "slot_ids": form["slot_ids"], "input_values": inputs}
    if samples:
        payload["number_of_samples"] = int(samples)
    print("form fields", [(f.get("key"), f.get("type"), f.get("required")) for f in form.get("fields") or []],
          "| samples", samples, "| inputs", inputs)

    code, msg, cards = _send(client, conv, "Review booking", {"type": "ba_review", "payload": payload})
    summary = _card(cards, "ba_booking_summary")
    if not check("review shows the booking summary", summary is not None,
                 f"cards {_types(cards)} | {str(msg.get('content') or '')[:200]}"):
        return
    confirm = [a for a in msg.get("suggested_actions") or [] if a.get("confirmation_token")]
    return list(form["slot_ids"]), summary, confirm


def _cancel_and_verify(client, student, booking, slot_ids, txn_start, before, debit_total):
    res = client.post(f"/api/bookings/{booking.booking_id}/user-cancel/",
                      {"refund": True, "notes": "Booking Assistant E2E test"}, format="json", secure=True)
    data = _data(res)
    booking.refresh_from_db()
    check("test booking cancelled through the normal cancel endpoint",
          res.status_code == 200 and booking.status in (BookingStatus.CANCELLED, BookingStatus.REFUNDED),
          f"http {res.status_code} status {booking.status} | {str(data.get('message') or data.get('error') or '')[:160]}")
    print("BOOKING_CANCELLED", booking.booking_id, booking.status, "refund_amount", data.get("refund_amount"))
    free = all(
        s.status == SlotStatus.AVAILABLE and not getattr(s, "booking_id", None)
        for s in DailySlot.objects.filter(pk__in=slot_ids)
    )
    check("slot is free again", free, str(list(DailySlot.objects.filter(pk__in=slot_ids).values_list("id", "status"))))
    credits = list(_student_txns(student, txn_start).filter(transaction_type="credit"))
    credit_total = sum((t.amount for t in credits), Decimal("0"))
    if debit_total == 0:
        check("no wallet charge was taken at booking, so nothing to refund", credit_total == 0, f"credits {credit_total}")
        return
    check("refund equals the booking debit", credit_total == debit_total, f"debit {debit_total} credit {credit_total}")
    after = {sid: SubWallet.objects.get(pk=sid).balance for sid in before}
    check("wallet balance back to its starting value", all(after[s] == before[s] for s in before),
          {s: f"{before[s]} -> {after[s]}" for s in before})


def main():
    with override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"), \
            patch.object(Task, "apply_async", lambda *a, **k: None):
        try:
            run()
        except Exception as exc:  # noqa: BLE001
            import traceback

            traceback.print_exc()
            check("test ran without an unexpected error", False, f"{type(exc).__name__}: {exc}")
    leftover = Booking.objects.filter(
        user__email__iexact=STUDENT_EMAIL, status__in=ACTIVE, created_at__gte=STARTED
    ).values_list("booking_id", flat=True)
    check("no active test booking left behind from this run", not list(leftover), list(leftover))
    failed = [name for name, ok in RESULTS if not ok]
    print("SUMMARY", len(RESULTS) - len(failed), "passed,", len(failed), "failed")
    sys.exit(1 if failed else 0)


main()
