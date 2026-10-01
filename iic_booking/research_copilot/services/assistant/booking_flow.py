"""
Booking on the user's behalf, always ending in an explicit confirmation.

inputs form (samples, booking-form fields, periodic table, extra sample sets) -> slot -> review -> summary.
The guided flow (`guided.py`) and the free-text path ("I need FESEM tomorrow" -> pick a time) both end
here, so they share the same input checks, pre-confirm rules and summary card.

Equipment whose required inputs can't be collected in chat (multi-select, tables, ICP-MS standard
coverage, STL uploads for 3D printing) is handed to the booking page pre-filled with the equipment,
date and whatever was already entered.

`review` runs the same checks the booking page and book endpoint run before showing a summary: input
types and numeric limits, sample sets, required analysis time vs the selected slots, then
`preflight.run` (freeze, department block, equipment status, I-STEM confirmation, charge profile, quota,
wallet balance, supervisor spending limit). Only then does `prepare_booking_create` store a short-lived
proposal bound to this user and payload. Nothing is booked here: the summary's "Confirm booking" button
posts the proposal id + token to /mutations/confirm/, which runs `_book_equipment_impl` exactly like
the booking page, with every check repeated under row locks.
"""

from __future__ import annotations

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
MAX_SAMPLE_SETS = 20


def _fields(user, eq) -> list:
    from iic_booking.equipment.equipment_group_service import _effective_input_fields

    try:
        return list(_effective_input_fields(eq, getattr(user, "user_type", "") or ""))
    except Exception:  # noqa: BLE001
        return []


def _numeric_bounds(f) -> dict[str, Any]:
    """Same min/max/step the book endpoint enforces (options, then help text, then defaults)."""
    from iic_booking.equipment.numeric_field_limits import resolve_numeric_field_bounds

    try:
        lo, hi, step = resolve_numeric_field_bounds(options=getattr(f, "options", None), help_text=getattr(f, "help_text", None))
    except Exception:  # noqa: BLE001
        return {}
    return {"min": lo, "max": hi, "step": step}


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
    from iic_booking.research_copilot.services.v2.mutations.booking import (
        CHAT_FILLABLE_FIELD_TYPES,
        _field_descriptor,
        periodic_field_info,
    )

    samples: dict[str, Any] | None = {"label": "Number of samples", "min": 1, "max": MAX_SAMPLES, "default": 1}
    out = []
    for f in _fields(user, eq):
        ftype = str(f.field_type)
        if f.field_key == "A" and ftype == "NUMERIC":
            bounds = _numeric_bounds(f)
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
            row.update(_numeric_bounds(f))
        elif ftype == "PERIODIC_TABLE":
            info = periodic_field_info(f)
            row.update({"allowed": info["allowed"], "locked": info["locked"], "default": None})
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


def _nav_actions(eq, *, slot: bool = True, inputs: bool = True) -> list[dict[str, Any]]:
    out = []
    if slot:
        out.append(C.flow_action("Change slot", "change_slot", {"equipment_id": int(eq.pk)}))
    if inputs:
        out.append(C.flow_action("Change samples/inputs", "edit_inputs", {"equipment_id": int(eq.pk)}))
    out.append(C.flow_action("Change equipment", "change_equipment"))
    out.append(C.flow_action("Cancel", "cancel"))
    return out


def _slot_retry(eq, text: str) -> dict[str, Any]:
    """Selected time can't be used; the guided flow turns this into a fresh slot list."""
    return C.reply(
        text,
        actions=[C.flow_action("Show free times", "change_slot", {"equipment_id": int(eq.pk)}, primary=True)]
        + _nav_actions(eq, slot=False),
        intent="book_slot_retry",
        extra={"equipment_id": int(eq.pk)},
    )


def _slot_problem_text(problem: str) -> str:
    if problem == "taken":
        return "That time was just taken by someone else."
    return "That time is not bookable for your account any more (booking window, department reservation or schedule change)."


def form_reply(
    user,
    eq,
    *,
    slot_ids: list[int] | None = None,
    values: dict[str, Any] | None = None,
    samples: int | None = None,
    sets: list[dict[str, Any]] | None = None,
    error: list[str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    """Step 3: the booking page's inputs for this equipment, collected in chat."""
    from iic_booking.equipment.models import EquipmentProfileType
    from iic_booking.research_copilot.services.assistant.info import instructions

    ids = [int(x) for x in (slot_ids or [])]
    summary: dict[str, Any] = {"label": "", "date": None}
    if ids:
        slots, _problem = _load_slots(user, eq, ids)
        if slots:
            summary = _slot_summary(slots)
        else:
            ids = []
    samples_spec, fields = _form_fields(user, eq)
    instruction = instructions(user, eq, limit=1500)
    dept = getattr(eq, "internal_department", None)
    card = {
        "type": "ba_booking_form",
        "flow": True,
        "step": C.step_info(3, "Samples & inputs"),
        "equipment_id": int(eq.pk),
        "equipment_name": eq.name,
        "department_name": getattr(dept, "name", "") or "",
        "slot_ids": ids,
        "slot_label": summary["label"],
        "date": summary["date"],
        "samples": samples_spec,
        "fields": fields,
        "instruction": instruction,
        "sample_sets": {
            "allowed": getattr(eq, "profile_type", None) != EquipmentProfileType.PRINT_3D and bool(fields or samples_spec),
            "max": MAX_SAMPLE_SETS,
        },
        "values": {**(values or {}), **({"_samples": samples} if samples else {})} or None,
        "sets_values": list(sets or []),
        "error": ("Please check: " + "; ".join(str(e) for e in error[:6])) if error else None,
        "submit_label": "Review booking" if ids else "Choose a slot",
        "booking_href": booking_href(eq.pk, summary["date"]),
        "prefill": {"equipment_id": int(eq.pk), "date": summary["date"], "input_values": dict(values or {})},
    }
    need = "the number of samples" if samples_spec else ""
    if fields:
        need = (need + " and " if need else "") + ("these details" if len(fields) > 1 else f"**{fields[0]['label']}**")
    head = f"**{eq.name}**" + (f" · {summary['label']}" if summary["label"] else "") + "."
    if error:
        content = "Some details need attention before I can continue."
    else:
        content = head + (f" Tell me {need}." if need else " No extra details are needed.")
        content += " Next I'll prepare the booking." if ids else " Next you'll pick a time slot."
    if note:
        content = note + "\n\n" + content
    return C.reply(
        content,
        cards=[card],
        actions=[C.flow_action("Change equipment", "change_equipment"), C.flow_action("Cancel", "cancel")],
        intent="book_form",
        title_hint=f"{eq.name} booking",
        extra={"equipment_id": int(eq.pk)},
    )


def pick_slot(user, eq, slot_ids: list[int]) -> dict[str, Any]:
    """A time picked from an availability answer: ask for the inputs next, keeping the slot."""
    from iic_booking.equipment.models import DailySlot

    first = DailySlot.objects.filter(pk=slot_ids[0], slot_master__equipment_id=eq.pk).only("date").first()
    day = first.date.isoformat() if first and first.date else None
    locked = _user_lock(user)
    if locked:
        return C.reply(locked, actions=[C.link("Open booking page", booking_href(eq.pk, day))], intent="book_slot")
    slots, problem = _load_slots(user, eq, slot_ids)
    if problem:
        return C.reply(
            _slot_problem_text(problem) + " Here are the times that are still free.",
            actions=[C.assistant_action("Show free times", "ba_availability",
                                        {"equipment_id": int(eq.pk), "when": {"start": day, "end": day} if day else None}, primary=True)],
            intent="book_slot",
        )
    complex_, blocking = is_complex(user, eq)
    if complex_:
        return handoff_reply(eq, _slot_summary(slots), reason=blocking)
    return form_reply(user, eq, slot_ids=[int(s.pk) for s in slots])


def _user_lock(user) -> str:
    try:
        from iic_booking.users.legacy_ledger.booking_lock import booking_is_locked

        locked, message = booking_is_locked(user)
    except Exception:  # noqa: BLE001
        return ""
    return (message or "Booking is temporarily locked for your account.") if locked else ""


def handoff_reply(
    eq,
    summary: dict[str, Any],
    *,
    reason: list[str] | None = None,
    input_values: dict[str, Any] | None = None,
    guided: bool = False,
) -> dict[str, Any]:
    href = booking_href(eq.pk, summary.get("date"))
    why = ", ".join((reason or [])[:4])
    content = f"**{eq.name}**" + (f" · {summary['label']}" if summary.get("label") else "") + "."
    content += (
        f" This instrument needs details I can't collect in chat ({why}), so I'll open the booking page with the"
        " equipment" + (" and date" if summary.get("date") else "") + " filled in."
        if why
        else " Continue on the booking page; the equipment and date are filled in."
    )
    if summary.get("label"):
        content += " Pick the same time there."
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
    if guided:
        card["step"] = C.step_info(3, "Samples & inputs")
    actions = [C.flow_action("Change equipment", "change_equipment"), C.flow_action("Cancel", "cancel")] if guided else []
    return C.reply(content, cards=[card], actions=actions, intent="book_handoff", title_hint=f"{eq.name} booking", extra={"equipment_id": int(eq.pk)})


def _fit_slots(user, eq, slots, needed: int) -> tuple[list[int] | None, str]:
    """Selected slots resized to the run length the inputs need (extended on the same day when free)."""
    ids = [int(s.pk) for s in slots]
    if needed <= len(ids):
        return ids[:needed], ("" if needed == len(ids) else "Shortened to the time your samples need.")
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

    fields = [f for f in _fields(user, eq) if str(f.field_type) in CHAT_FILLABLE_FIELD_TYPES]
    allowed = {f.field_key for f in fields}
    allowed |= {f"{f.field_key}_elements" for f in fields if str(f.field_type) == "PERIODIC_TABLE"}
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        if k in allowed and v not in (None, ""):
            out[k] = str(v)[:500]
    return out


def _sample_count(value: Any) -> int:
    try:
        return max(1, min(int(float(str(value or 1))), MAX_SAMPLES))
    except (TypeError, ValueError):
        return 1


def check_inputs(user, eq, samples: int, raw_inputs: dict[str, Any], raw_sets: list[dict[str, Any]] | None) -> dict[str, Any]:
    """
    Booking page input rules, server-side: field types/options, required fields (0 counts as empty),
    numeric limits, periodic-table symbols, each extra sample set, then the analysis time from the
    portal calculator (which also rejects inputs that can't be priced for this user).
    """
    from iic_booking.equipment.api_views import _normalize_sample_sets_input
    from iic_booking.research_copilot.services.v2.mutations.booking import _copilot_input_values, required_analysis_minutes

    samples = _sample_count(samples)
    provided = _clean_inputs(user, eq, raw_inputs)
    values, problems = _copilot_input_values(user=user, equipment=eq, samples=samples, provided=provided, validate=True)
    errors: list[str] = [str(p) for p in problems] if values is None else []
    sets_raw: list[dict[str, Any]] = []
    sets_full: list[dict[str, Any]] = []
    for i, raw in enumerate((raw_sets or [])[:MAX_SAMPLE_SETS]):
        s_samples = _sample_count(raw.get("A"))
        s_provided = _clean_inputs(user, eq, raw)
        sets_raw.append({**s_provided, "A": str(s_samples)})
        s_values, s_problems = _copilot_input_values(user=user, equipment=eq, samples=s_samples, provided=s_provided, validate=True)
        if s_values is None:
            errors.extend(f"Sample set {i + 2}: {p}" for p in s_problems[:3])
        else:
            sets_full.append(s_values)
    full: dict[str, Any] = dict(values or {})
    required = None
    if not errors and sets_full:
        full, sets_error = _normalize_sample_sets_input(eq, full, sets_full, booking_user=user)
        if sets_error:
            errors.append(str(sets_error))
    if not errors:
        required, calc_error = required_analysis_minutes(user=user, equipment=eq, input_values=full)
        if calc_error:
            errors.append(calc_error)
    return {
        "samples": samples,
        "provided": provided,
        "sets_raw": sets_raw,
        "sets_full": sets_full,
        "values": full,
        "required_minutes": required,
        "errors": errors,
    }


def _money(v: Any) -> str:
    try:
        return f"₹{float(v):,.2f}"
    except (TypeError, ValueError):
        return "—"


def blocked_reply(eq, pf: dict[str, Any]) -> dict[str, Any]:
    lines = "\n".join(f"- {p}" for p in pf["problems"])
    content = f"I can't prepare this **{eq.name}** booking yet:\n{lines}"
    charge = pf.get("charge") or {}
    if charge.get("total") is not None:
        content += f"\n\nThis booking would cost **{_money(charge['total'])}**."
    actions = _nav_actions(eq)
    text = " ".join(pf["problems"]).lower()
    if any(w in text for w in ("wallet", "balance", "credit", "recharge")):
        actions.insert(0, C.link("Open Wallet", "/wallet"))
    if "profile" in text and "i-stem" in text:
        actions.insert(0, C.link("Open Profile", "/profile"))
    return C.reply(content, actions=actions, intent="book_blocked", extra={"equipment_id": int(eq.pk)})


def review(
    user,
    eq,
    slot_ids: list[int],
    samples: int,
    raw_inputs: dict[str, Any],
    raw_sets: list[dict[str, Any]] | None = None,
    *,
    guided: bool = False,
) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import preflight
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    locked = _user_lock(user)
    if locked:
        return C.reply(locked, actions=[C.flow_action("Cancel", "cancel")], intent="book_review")
    slots, problem = _load_slots(user, eq, slot_ids)
    if problem:
        return _slot_retry(eq, _slot_problem_text(problem) + " Pick another time.")
    summary = _slot_summary(slots)
    complex_, blocking = is_complex(user, eq)
    if complex_:
        return handoff_reply(eq, summary, reason=blocking, guided=guided)
    checked = check_inputs(user, eq, samples, raw_inputs, raw_sets)
    if checked["errors"]:
        return form_reply(
            user, eq, slot_ids=[int(s.pk) for s in slots], values=checked["provided"],
            samples=checked["samples"], sets=checked["sets_raw"], error=checked["errors"],
        )
    samples = checked["samples"]
    provided = checked["provided"]
    required = checked["required_minutes"]
    needed = slots_needed(eq, required)
    fitted, fit_note = _fit_slots(user, eq, slots, needed)
    if fitted is None:
        minutes = f" (about {int(required)} minutes)" if required else ""
        return _slot_retry(
            eq,
            f"Your samples need {needed} back-to-back slot{'s' if needed != 1 else ''}{minutes}, and the time you picked "
            "doesn't have that many free in a row. Pick another start time.",
        )
    fitted_slots, problem = _load_slots(user, eq, fitted)
    if problem:
        return _slot_retry(eq, _slot_problem_text(problem) + " Pick another time.")

    pf = preflight.run(user, eq, checked["values"], fitted_slots)
    if not pf["ok"]:
        return blocked_reply(eq, pf)

    prep = booking_mut.prepare_booking_create(
        user=user, equipment_id=int(eq.pk), slot_ids=fitted, number_of_samples=samples,
        input_values=provided, sample_sets=checked["sets_full"] or None,
    )
    if not prep.get("ok"):
        if prep.get("error") in ("SLOT_SELECTION_MISMATCH", "SLOT_NOT_BOOKABLE", "SLOT_NOT_FOUND", "SLOT_UNAVAILABLE"):
            return _slot_retry(eq, prep.get("message") or "Pick another time.")
        return C.reply(prep.get("message") or "I couldn't prepare that booking.", actions=_nav_actions(eq), intent="book_review")
    status = prep.get("status")
    if status == "NEEDS_PORTAL_FORM":
        return handoff_reply(eq, summary, reason=list(prep.get("missing_inputs") or []), input_values=provided, guided=guided)
    if status != "READY_FOR_CONFIRMATION":
        return C.reply(prep.get("message") or "Pick a slot to continue.", actions=_nav_actions(eq), intent="book_review")
    return summary_reply(
        user, eq, prep, provided=provided, fit_note=fit_note, pf=pf,
        sets_count=len(checked["sets_full"]), required_minutes=required,
    )


def summary_reply(
    user,
    eq,
    prep: dict[str, Any],
    *,
    provided: dict[str, Any],
    fit_note: str = "",
    pf: dict[str, Any] | None = None,
    sets_count: int = 0,
    required_minutes: Any = None,
) -> dict[str, Any]:
    """Step 5: everything the booking will do, and the only Confirm button (server-issued, single-use)."""
    from iic_booking.research_copilot.services.assistant.info import input_fields, instructions

    pf = pf or {}
    charge = pf.get("charge") or {}
    wallet = pf.get("wallet") or {}
    labels = {f["key"]: f["label"] for f in input_fields(user, eq)}
    shown_inputs = [
        {"key": k, "label": labels.get(k, k), "value": v}
        for k, v in sorted((prep.get("input_values") or {}).items())
        if not str(k).startswith("_") and not str(k).endswith("_elements")
    ]
    for k, v in sorted((prep.get("input_values") or {}).items()):
        if str(k).endswith("_elements") and v:
            base = str(k)[: -len("_elements")]
            for row in shown_inputs:
                if row["key"] == base:
                    row["value"] = f"{v} ({row['value']} billable)"
    start, end = _local(prep.get("start_time")), _local(prep.get("end_time"))
    when_label = f"{start:%a %d %b %Y}, {start:%H:%M}" + (f"–{end:%H:%M}" if end else "") if start else ""
    cancel_note, cancel_until = booking_mut_cancel_note(eq, prep)
    executable = bool(prep.get("executable") and prep.get("proposal_id") and prep.get("confirmation_token"))
    if not executable and prep.get("proposal_id"):
        from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store

        prop_store.invalidate_proposal(prep["proposal_id"])
        prep = {**prep, "proposal_id": None, "confirmation_token": None}
    instruction = instructions(user, eq, limit=1500)
    amount_due = wallet.get("amount_due") or 0
    notes = [n for n in (fit_note,) if n]
    if amount_due and amount_due > 0:
        notes.append(f"{_money(amount_due)} is paid online after you confirm; the booking is held until payment completes.")
    dept = getattr(eq, "internal_department", None)
    card = {
        "type": "ba_booking_summary",
        "title": "Booking summary",
        "step": C.step_info(5, "Review & confirm"),
        "proposal_id": prep.get("proposal_id"),
        "executable": executable,
        "expires_at": prep.get("expires_at"),
        "equipment_id": int(eq.pk),
        "equipment_name": eq.name,
        "department_name": getattr(dept, "name", "") or "",
        "date": prep.get("date"),
        "start_time": prep.get("start_time"),
        "end_time": prep.get("end_time"),
        "when_label": when_label,
        "slot_count": len(prep.get("slot_ids") or []),
        "slot_minutes": charge.get("slot_minutes"),
        "required_minutes": required_minutes,
        "sample_count": prep.get("sample_count"),
        "sample_sets": sets_count,
        "inputs": shown_inputs,
        "estimated_amount": charge.get("total", prep.get("estimated_amount")),
        "charge": charge.get("charge"),
        "gst_percent": charge.get("gst_percent"),
        "gst_amount": charge.get("gst_amount"),
        "total_amount": charge.get("total"),
        "charge_lines": charge.get("breakdown") or [],
        "wallet_label": wallet.get("label"),
        "wallet_balance": wallet.get("balance"),
        "balance_after_total": pf.get("balance_after"),
        "amount_due": amount_due or None,
        "spending": pf.get("spending"),
        "cancellation_policy_note": cancel_note,
        "cancellable_until": cancel_until,
        "instruction": instruction,
        "instruction_ack_required": bool(instruction),
        "warnings": [],
        "notes": notes,
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
        if instruction:
            content += " Please read the instructions and tick the box first."
    else:
        content = (
            "Here's the booking summary. Booking straight from chat isn't switched on for your account yet, "
            "so continue on the booking page; the equipment, date and your inputs are filled in."
        )
        actions.append(C.link("Open booking page", card["booking_href"], primary=True))
    actions.extend(_nav_actions(eq))
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
