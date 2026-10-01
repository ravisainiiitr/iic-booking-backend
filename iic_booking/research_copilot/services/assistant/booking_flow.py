"""
Booking on the user's behalf, always ending in an explicit confirmation.

slot pick -> (simple form in chat | pre-filled deep link to the booking page) -> review -> summary card.

"Simple" equipment is one whose required booking inputs are all chat-fillable (numeric, text, radio,
dropdown, toggle) and which is not a 3D-print profile. Anything else (multi-select, periodic table,
tables, ICP-MS standard coverage, STL uploads) is handed to the booking page pre-filled with the
equipment and date, because those widgets cannot be reproduced faithfully in chat.

The review step calls `prepare_booking_create`, which re-checks the slots against the portal rules
and stores a short-lived proposal bound to this user and payload. Nothing is booked here: the
summary's "Confirm booking" button posts the proposal id + token to /mutations/confirm/, which runs
`_book_equipment_impl` (visibility, locks, department blocks, input limits, wallet, quotas and
student spending limits) exactly like the booking page.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant.availability import (
    _local,
    _lookup,
    _runs,
    booking_href,
    chip,
    slots_needed,
)

MAX_SAMPLES = 500


def _fields(user, eq) -> list:
    from iic_booking.equipment.equipment_group_service import _effective_input_fields

    try:
        return list(_effective_input_fields(eq, getattr(user, "user_type", "") or ""))
    except Exception:  # noqa: BLE001
        return []


def _numeric_bounds(help_text: str | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lines = [l.strip() for l in str(help_text or "").splitlines()]
    for key, idx in (("min", 0), ("max", 1), ("step", 2)):
        if len(lines) > idx and lines[idx]:
            try:
                out[key] = float(lines[idx])
            except ValueError:
                pass
    return out


def is_complex(user, eq) -> tuple[bool, list[str]]:
    from iic_booking.equipment.models import EquipmentProfileType
    from iic_booking.research_copilot.services.v2.mutations.booking import CHAT_FILLABLE_FIELD_TYPES

    if getattr(eq, "profile_type", None) == EquipmentProfileType.PRINT_3D:
        return True, ["3D print model upload"]
    blocking = [
        f.field_label or f.field_key
        for f in _fields(user, eq)
        if f.is_required and f.default_value in (None, "") and str(f.field_type) not in CHAT_FILLABLE_FIELD_TYPES
    ]
    return bool(blocking), blocking


def _form_fields(user, eq) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    from iic_booking.research_copilot.services.v2.mutations.booking import CHAT_FILLABLE_FIELD_TYPES, _field_descriptor

    samples: dict[str, Any] | None = {"label": "Number of samples", "min": 1, "max": MAX_SAMPLES, "default": 1}
    out = []
    for f in _fields(user, eq):
        ftype = str(f.field_type)
        if f.field_key == "A" and ftype == "NUMERIC":
            bounds = _numeric_bounds(f.help_text)
            samples = {
                "label": f.field_label or "Number of samples",
                "min": max(1, int(bounds.get("min", 1) or 1)),
                "max": int(min(bounds.get("max", MAX_SAMPLES) or MAX_SAMPLES, MAX_SAMPLES)),
                "default": 1,
            }
            continue
        if f.field_key == "A":
            samples = None
        if ftype not in CHAT_FILLABLE_FIELD_TYPES:
            continue
        d = _field_descriptor(f)
        row = {
            "key": d["key"],
            "label": d["label"],
            "type": ftype,
            "required": d["required"],
            "options": d["options"],
            "default": f.default_value if f.default_value not in (None, "") else None,
        }
        if ftype == "NUMERIC":
            row.update(_numeric_bounds(f.help_text))
        else:
            row["help"] = d["help"]
        out.append(row)
    return samples, out


def _slot_summary(slots) -> dict[str, Any]:
    start = slots[0].start_datetime
    end = slots[-1].end_datetime
    ls = timezone.localtime(start) if start else None
    le = timezone.localtime(end) if end else None
    label = f"{ls:%a %d %b}, {ls:%H:%M}" + (f"–{le:%H:%M}" if le else "") if ls else ""
    return {"label": label, "date": ls.date().isoformat() if ls else None}


def _load_slots(user, eq, slot_ids: list[int]):
    from iic_booking.research_copilot.services.v2.mutations.booking import _load_slot, _slots_bookable_for_user

    slots = []
    for sid in slot_ids:
        slot, err = _load_slot(slot_id=int(sid), equipment_id=int(eq.pk))
        if err:
            return None, "taken"
        slots.append(slot)
    slots.sort(key=lambda s: s.start_datetime or timezone.now())
    if not _slots_bookable_for_user(user=user, equipment_id=int(eq.pk), slot_ids=[int(s.pk) for s in slots]):
        return None, "not_bookable"
    return slots, None


def _slot_problem_reply(eq, problem: str, day: str | None) -> dict[str, Any]:
    text = (
        "That time was just taken by someone else."
        if problem == "taken"
        else "That time is not bookable for your account any more (booking window, department reservation or schedule change)."
    )
    payload = {"equipment_id": int(eq.pk), "when": {"start": day, "end": day} if day else None}
    return C.reply(
        text + " Here are the times that are still free.",
        actions=[C.assistant_action("Show free times", "ba_availability", payload, primary=True)],
        intent="book_slot",
    )


def pick_slot(user, eq, slot_ids: list[int]) -> dict[str, Any]:
    from iic_booking.equipment.models import DailySlot

    first = DailySlot.objects.filter(pk=slot_ids[0], slot_master__equipment_id=eq.pk).only("date").first()
    day = first.date.isoformat() if first and first.date else None
    locked = _user_lock(user)
    if locked:
        return C.reply(locked, actions=[C.link("Open booking page", booking_href(eq.pk, day))], intent="book_slot")
    slots, problem = _load_slots(user, eq, slot_ids)
    if problem:
        return _slot_problem_reply(eq, problem, day)
    summary = _slot_summary(slots)
    complex_, blocking = is_complex(user, eq)
    if complex_:
        return handoff_reply(eq, summary, reason=blocking)
    samples, fields = _form_fields(user, eq)
    card = {
        "type": "ba_booking_form",
        "equipment_id": int(eq.pk),
        "equipment_name": eq.name,
        "slot_ids": [int(s.pk) for s in slots],
        "slot_label": summary["label"],
        "date": summary["date"],
        "samples": samples,
        "fields": fields,
        "submit_label": "Review booking",
        "booking_href": booking_href(eq.pk, summary["date"]),
    }
    need = "the number of samples" if samples else ""
    if fields:
        need = (need + " and " if need else "") + ("these details" if len(fields) > 1 else f"**{fields[0]['label']}**")
    content = f"**{eq.name}** · {summary['label']}."
    content += f" Tell me {need}, then review the booking." if need else " Review the booking next."
    return C.reply(content, cards=[card], intent="book_form", title_hint=f"{eq.name} booking", extra={"equipment_id": int(eq.pk)})


def _user_lock(user) -> str:
    try:
        from iic_booking.users.legacy_ledger.booking_lock import booking_is_locked

        locked, message = booking_is_locked(user)
    except Exception:  # noqa: BLE001
        return ""
    return (message or "Booking is temporarily locked for your account.") if locked else ""


def handoff_reply(eq, summary: dict[str, Any], *, reason: list[str] | None = None, input_values: dict[str, Any] | None = None) -> dict[str, Any]:
    href = booking_href(eq.pk, summary.get("date"))
    why = ", ".join((reason or [])[:4])
    content = f"**{eq.name}** · {summary.get('label') or ''}.".strip()
    content += (
        f" This instrument needs details I can't collect in chat ({why}), so I'll open the booking page with the"
        " equipment and date filled in. Pick the same time there."
        if why
        else " Continue on the booking page; the equipment and date are filled in."
    )
    card = {
        "type": "ba_booking_handoff",
        "equipment_id": int(eq.pk),
        "equipment_name": eq.name,
        "slot_label": summary.get("label") or "",
        "date": summary.get("date"),
        "reason": reason or [],
        "href": href,
        "prefill": {"equipment_id": int(eq.pk), "date": summary.get("date"), "input_values": input_values or {}},
    }
    return C.reply(content, cards=[card], intent="book_handoff", title_hint=f"{eq.name} booking", extra={"equipment_id": int(eq.pk)})


def _wallet_info(user, equipment) -> dict[str, Any]:
    info: dict[str, Any] = {"label": "Your wallet", "balance": None}
    try:
        wallet = user.get_accessible_wallet() if hasattr(user, "get_accessible_wallet") else None
    except Exception:  # noqa: BLE001
        wallet = None
    if wallet is None:
        info["label"] = "No wallet linked"
        return info
    owner = getattr(wallet, "user", None)
    if owner is not None and int(getattr(owner, "pk", 0) or 0) != int(user.pk):
        info["label"] = f"{getattr(owner, 'name', '') or getattr(owner, 'email', 'Supervisor')}'s wallet"
    try:
        info["balance"] = float(wallet.total_balance)
    except Exception:  # noqa: BLE001
        info["balance"] = None
    return info


def _fit_slots(user, eq, slots, needed: int) -> tuple[list[int] | None, str]:
    """Selected slots resized to the run length the inputs need (extended on the same day when free)."""
    ids = [int(s.pk) for s in slots]
    if needed <= len(ids):
        return ids[:needed], ""
    day = slots[0].date
    lookup = _lookup(user, eq, day, day)
    if not lookup.ok:
        return None, ""
    for run in _runs(lookup.rows, needed):
        if int(run[0]["slot_id"]) == ids[0]:
            c = chip(run)
            return c["slot_ids"], f"Extended to {c['label']} so the run fits your samples."
    return None, ""


def _clean_inputs(user, eq, raw: dict[str, Any]) -> dict[str, str]:
    from iic_booking.research_copilot.services.v2.mutations.booking import CHAT_FILLABLE_FIELD_TYPES

    allowed = {f.field_key: f for f in _fields(user, eq) if str(f.field_type) in CHAT_FILLABLE_FIELD_TYPES}
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        if k in allowed and v not in (None, ""):
            out[k] = str(v)[:500]
    return out


def review(user, eq, slot_ids: list[int], samples: int, raw_inputs: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import flows
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    locked = _user_lock(user)
    if locked:
        return C.reply(locked, intent="book_review")
    slots, problem = _load_slots(user, eq, slot_ids)
    if problem:
        return _slot_problem_reply(eq, problem, None)
    summary = _slot_summary(slots)
    complex_, blocking = is_complex(user, eq)
    if complex_:
        return handoff_reply(eq, summary, reason=blocking)
    samples = max(1, min(int(samples or 1), MAX_SAMPLES))
    provided = _clean_inputs(user, eq, raw_inputs)
    values, problems = booking_mut._copilot_input_values(user=user, equipment=eq, samples=samples, provided=provided, validate=True)
    if values is None:
        reply_ = pick_slot(user, eq, [int(s.pk) for s in slots])
        for card in reply_.get("cards") or []:
            if card.get("type") == "ba_booking_form":
                card["error"] = "Please check: " + "; ".join(str(p) for p in problems[:5])
                card["values"] = {**provided, "_samples": samples}
        reply_["content"] = "Some details need attention before I can prepare the booking."
        return reply_

    estimate = flows.estimate(user, eq, samples, provided) or {}
    needed = slots_needed(eq, estimate.get("total_time_minutes"))
    fitted, fit_note = _fit_slots(user, eq, slots, needed)
    if fitted is None:
        day = summary["date"]
        return C.reply(
            f"{samples} sample{'s' if samples != 1 else ''} need about {needed} back-to-back slots, and the time you picked "
            "doesn't have that many free in a row. Pick another start time.",
            actions=[C.assistant_action("Show free times", "ba_availability",
                                        {"equipment_id": int(eq.pk), "when": {"start": day, "end": day}}, primary=True)],
            intent="book_review",
        )

    prep = booking_mut.prepare_booking_create(
        user=user, equipment_id=int(eq.pk), slot_ids=fitted, number_of_samples=samples, input_values=provided
    )
    if not prep.get("ok"):
        return C.reply(
            prep.get("message") or "I couldn't prepare that booking.",
            actions=[C.assistant_action("Show free times", "ba_availability",
                                        {"equipment_id": int(eq.pk), "when": {"start": summary["date"], "end": summary["date"]}})],
            intent="book_review",
        )
    status = prep.get("status")
    if status == "NEEDS_PORTAL_FORM":
        return handoff_reply(eq, summary, reason=list(prep.get("missing_inputs") or []), input_values=provided)
    if status != "READY_FOR_CONFIRMATION":
        return C.reply(prep.get("message") or "Pick a slot to continue.", intent="book_review")
    return summary_reply(user, eq, prep, provided=provided, fit_note=fit_note)


def summary_reply(user, eq, prep: dict[str, Any], *, provided: dict[str, Any], fit_note: str = "") -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import flows
    from iic_booking.research_copilot.services.assistant.info import input_fields

    breakdown = flows.charge_breakdown(user, prep.get("estimated_amount"))
    wallet = _wallet_info(user, eq)
    balance = wallet.get("balance")
    if balance is None and prep.get("wallet_balance") is not None:
        try:
            balance = float(prep["wallet_balance"])
        except (TypeError, ValueError):
            balance = None
    after = None
    if balance is not None and breakdown.get("total") is not None:
        after = float(Decimal(str(balance)) - Decimal(str(breakdown["total"])))
    labels = {f["key"]: f["label"] for f in input_fields(user, eq)}
    shown_inputs = [
        {"key": k, "label": labels.get(k, k), "value": v}
        for k, v in sorted((prep.get("input_values") or {}).items())
        if not str(k).startswith("_")
    ]
    start, end = _local(prep.get("start_time")), _local(prep.get("end_time"))
    when_label = f"{start:%a %d %b %Y}, {start:%H:%M}" + (f"–{end:%H:%M}" if end else "") if start else ""
    cancel_note, _until = booking_mut_cancel_note(eq, prep)
    executable = bool(prep.get("executable") and prep.get("proposal_id") and prep.get("confirmation_token"))
    if not executable and prep.get("proposal_id"):
        from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store

        prop_store.invalidate_proposal(prep["proposal_id"])
        prep = {**prep, "proposal_id": None, "confirmation_token": None}
    warnings: list[str] = []
    if after is not None and after < 0:
        warnings.append("The estimated total is more than the wallet balance; the booking will fail unless the wallet is recharged.")
    card = {
        "type": "ba_booking_summary",
        "title": "Booking summary",
        "proposal_id": prep.get("proposal_id"),
        "executable": executable,
        "expires_at": prep.get("expires_at"),
        "equipment_id": int(eq.pk),
        "equipment_name": eq.name,
        "date": prep.get("date"),
        "start_time": prep.get("start_time"),
        "end_time": prep.get("end_time"),
        "when_label": when_label,
        "slot_count": len(prep.get("slot_ids") or []),
        "sample_count": prep.get("sample_count"),
        "inputs": shown_inputs,
        "estimated_amount": prep.get("estimated_amount"),
        "charge": breakdown.get("charge"),
        "gst_percent": breakdown.get("gst_percent"),
        "gst_amount": breakdown.get("gst_amount"),
        "total_amount": breakdown.get("total"),
        "wallet_label": wallet.get("label"),
        "wallet_balance": balance,
        "balance_after_total": after,
        "cancellation_policy_note": cancel_note,
        "warnings": warnings,
        "notes": [n for n in (fit_note,) if n],
        "booking_href": booking_href(eq.pk, prep.get("date")),
        "prefill": {"equipment_id": int(eq.pk), "date": prep.get("date"), "input_values": provided},
    }
    actions: list[dict[str, Any]] = []
    if executable:
        actions.append({
            "id": f"ba_confirm:{prep.get('proposal_id')}",
            "label": "Confirm booking",
            "proposal_id": prep.get("proposal_id"),
            "confirmation_token": prep.get("confirmation_token"),
            "mutation_action": "CREATE_BOOKING",
            "requires_confirmation": True,
            "confirmation_required": True,
            "primary": True,
            "enabled": True,
        })
        content = "Here's the booking. Check the details, then press **Confirm booking**; nothing is booked until you do."
    else:
        content = (
            "Here's the booking summary. Booking straight from chat isn't switched on for your account yet, "
            "so continue on the booking page; the equipment, date and your inputs are filled in."
        )
    actions.append(C.assistant_action(
        "Change details", "ba_pick_slot", {"equipment_id": int(eq.pk), "slot_ids": [int(x) for x in prep.get("slot_ids") or []]},
    ))
    actions.append(C.assistant_action(
        "Pick another time", "ba_availability",
        {"equipment_id": int(eq.pk), "when": {"start": prep.get("date"), "end": prep.get("date")} if prep.get("date") else None},
    ))
    if warnings:
        actions.append(C.link("Open Wallet", "/wallet"))
    return C.reply(
        content,
        cards=[card],
        actions=actions,
        intent="book_summary",
        title_hint=f"{eq.name} booking",
        extra={"equipment_id": int(eq.pk), "executable": executable, "proposal_id": prep.get("proposal_id")},
    )


def booking_mut_cancel_note(eq, prep: dict[str, Any]) -> tuple[str, str | None]:
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    start = _local(prep.get("start_time"))
    try:
        return booking_mut._cancellation_window_note(equipment=eq, start=start)
    except Exception:  # noqa: BLE001
        return "", None
