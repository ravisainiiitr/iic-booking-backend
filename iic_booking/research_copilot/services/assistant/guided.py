"""
Guided "Book equipment" flow: department -> equipment -> booking inputs -> slot -> summary (Step n of 5).

Departments and equipment come from the user's own visible catalogue (`get_visible_equipment_queryset`:
private groups, hidden department catalogues, multi-mode rules), limited to equipment that is ACTIVE,
priced for the user's category and in a department that currently accepts bookings. Multi-mode
equipment is listed like the portal catalogue: the parent with its modes underneath.

State lives in the assistant cache under `flow` so "Change slot / inputs / equipment" keep what the
user already entered. Each step re-reads equipment under the user's visibility; nothing is trusted
from the client except ids that are looked up again. The summary step runs the same pre-confirm
checks and proposal as the free-text path (`booking_flow.review`).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant import state as ba_state

MAX_EQUIPMENT_SHOWN = 40
PRICE_HINTS = 12
WINDOW_DAYS = 7


# --------------------------------------------------------------------------------------------- state

def load(conversation) -> dict[str, Any]:
    flow = ba_state.load(conversation).get("flow")
    return dict(flow) if isinstance(flow, dict) else {}


def save(conversation, **values: Any) -> dict[str, Any]:
    flow = load(conversation)
    flow.update(values)
    ba_state.save(conversation, flow=flow)
    return flow


def clear(conversation) -> None:
    ba_state.save(conversation, flow={}, pending_proposal_id="")


def active(conversation) -> bool:
    return bool(load(conversation).get("step"))


def _reset_intelligence(conversation) -> None:
    """The older intelligence flow must not also answer while this one is running."""
    try:
        from iic_booking.research_copilot.services.intelligence import state as intel_state

        st = intel_state.load(conversation)
        if st.get("step") or st.get("pending_choice") or st.get("workflow"):
            intel_state.save(conversation, intel_state.reset_workflow(st))
    except Exception:  # noqa: BLE001
        pass


# ------------------------------------------------------------------------------------- catalogue

def bookable_equipment(user) -> list:
    """Equipment this user could book right now, in catalogue order."""
    from iic_booking.equipment.api_views import get_visible_equipment_queryset
    from iic_booking.equipment.models import ChargeProfile, EquipmentStatus
    from iic_booking.users.legacy_ledger.booking_lock import department_equipment_booking_blocked

    qs = (
        get_visible_equipment_queryset(user)
        .filter(status=EquipmentStatus.ACTIVE)
        .select_related("internal_department", "category", "parent_equipment")
        .order_by("name")
    )
    user_type = getattr(user, "user_type", "") or ""
    priced = set(
        ChargeProfile.objects.filter(is_active=True, user_type=user_type).values_list("equipment_id", flat=True)
    )
    blocked_by_dept: dict[Any, bool] = {}
    out = []
    for eq in qs:
        if eq.pk not in priced:
            continue
        key = eq.internal_department_id
        if key not in blocked_by_dept:
            blocked, _msg = department_equipment_booking_blocked(eq, user)
            blocked_by_dept[key] = bool(blocked)
        if blocked_by_dept[key]:
            continue
        out.append(eq)
    return out


def _dept_label(dept) -> str:
    if dept is None:
        return "Other"
    return str(getattr(dept, "name", "") or getattr(dept, "code", "") or "Department")


def _equipment_row(eq, modes: list, hint: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "equipment_id": int(eq.pk),
        "name": eq.name,
        "code": eq.code or "",
        "category": getattr(getattr(eq, "category", None), "name", "") or "",
        "location": (eq.location or "")[:120],
        "description": " ".join(str(eq.description or "").split())[:140],
        "price_hint": hint,
        "modes": [{"equipment_id": int(m.pk), "name": m.name} for m in modes],
    }


def _price_hint(user, eq) -> dict[str, Any] | None:
    from iic_booking.research_copilot.services.assistant.availability import estimate_one_sample

    try:
        est = estimate_one_sample(user, eq)
    except Exception:  # noqa: BLE001
        return None
    if est.get("total") is None:
        return None
    return {"total": est.get("total"), "gst_included": bool(est.get("gst_amount")), "slots_needed": est.get("slots_needed")}


def _visible_equipment(user, equipment_id):
    from iic_booking.research_copilot.services.assistant.engine import _visible

    return _visible(user, equipment_id) if equipment_id else None


# ------------------------------------------------------------------------------------------- steps

def departments_reply(user, conversation, *, note: str = "") -> dict[str, Any]:
    _reset_intelligence(conversation)
    eqs = bookable_equipment(user)
    if not eqs:
        clear(conversation)
        return C.reply(
            (note + "\n\n" if note else "")
            + "There is no equipment open for booking for your account right now. "
            "Bookings may be paused for your category or department; check again later or browse the catalogue.",
            actions=[C.link("Browse equipment", "/equipments")],
            intent="flow_departments",
        )
    groups: dict[int, dict[str, Any]] = {}
    for eq in eqs:
        dept = getattr(eq, "internal_department", None)
        key = int(getattr(dept, "pk", 0) or 0)
        row = groups.setdefault(key, {"department_id": key, "name": _dept_label(dept), "code": getattr(dept, "code", "") or "", "count": 0})
        if eq.parent_equipment_id is None or eq.parent_equipment_id not in {e.pk for e in eqs}:
            row["count"] += 1
    items = sorted(groups.values(), key=lambda r: r["name"].lower())
    own = getattr(user, "department_id", None)
    if own:
        items.sort(key=lambda r: 0 if r["department_id"] == own else 1)
    if len(items) == 1:
        only = items[0]
        return equipment_reply(
            user, conversation, only["department_id"],
            note=(note + "\n\n" if note else "") + f"Only **{only['name']}** has equipment you can book right now.",
        )
    save(conversation, step="department", department_id=None, equipment_id=None)
    card = {
        "type": "ba_flow_departments",
        "title": "Choose a department",
        "step": C.step_info(1, "Department"),
        "items": items,
    }
    content = (note + "\n\n" if note else "") + "Which department's equipment do you want to book?"
    return C.reply(
        content,
        cards=[card],
        actions=[C.flow_action("Cancel", "cancel")],
        intent="flow_departments",
        title_hint="Book equipment",
    )


def equipment_reply(user, conversation, department_id: int | None, *, note: str = "") -> dict[str, Any]:
    eqs = bookable_equipment(user)
    dept_id = int(department_id or 0)
    in_dept = [e for e in eqs if int(e.internal_department_id or 0) == dept_id]
    if not in_dept:
        return departments_reply(user, conversation, note="That department has no equipment you can book right now.")
    ids = {e.pk for e in in_dept}
    children: dict[int, list] = {}
    tops = []
    for e in in_dept:
        if e.parent_equipment_id and e.parent_equipment_id in ids:
            children.setdefault(int(e.parent_equipment_id), []).append(e)
        else:
            tops.append(e)
    shown = tops[:MAX_EQUIPMENT_SHOWN]
    rows = [
        _equipment_row(e, children.get(int(e.pk), []), _price_hint(user, e) if i < PRICE_HINTS else None)
        for i, e in enumerate(shown)
    ]
    dept_name = _dept_label(getattr(in_dept[0], "internal_department", None))
    save(conversation, step="equipment", department_id=dept_id, equipment_id=None)
    card = {
        "type": "ba_flow_equipment",
        "title": f"{dept_name} equipment",
        "department_id": dept_id,
        "department_name": dept_name,
        "step": C.step_info(2, "Equipment"),
        "items": rows,
        "more": max(0, len(tops) - len(shown)),
    }
    content = (note + "\n\n" if note else "") + f"Which **{dept_name}** instrument do you want to book?"
    if card["more"]:
        content += f" Showing {len(shown)} of {len(tops)}; type the instrument name if it isn't listed."
    return C.reply(
        content,
        cards=[card],
        actions=[C.flow_action("Back to departments", "change_department"), C.flow_action("Cancel", "cancel")],
        intent="flow_equipment",
        title_hint=f"Book {dept_name} equipment",
    )


def inputs_reply(user, conversation, eq, *, note: str = "", error: list[str] | None = None) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import booking_flow, preflight

    flow = load(conversation)
    same = int(flow.get("equipment_id") or 0) == int(eq.pk)
    problems = preflight.account_problems(user, eq)
    if problems:
        return C.reply(
            f"**{eq.name}** can't be booked from your account right now:\n" + "\n".join(f"- {p}" for p in problems),
            actions=[C.flow_action("Change equipment", "change_equipment"), C.flow_action("Cancel", "cancel")],
            intent="flow_blocked",
        )
    save(
        conversation,
        step="inputs",
        equipment_id=int(eq.pk),
        department_id=int(eq.internal_department_id or 0),
        **({} if same else {"samples": None, "inputs": {}, "sets": [], "slot_ids": [], "required_minutes": None}),
    )
    flow = load(conversation)
    complex_, blocking = booking_flow.is_complex(user, eq)
    if complex_:
        return booking_flow.handoff_reply(
            eq, {"label": "", "date": None}, reason=blocking, input_values=flow.get("inputs") or {}, guided=True,
        )
    return booking_flow.form_reply(
        user, eq,
        slot_ids=flow.get("slot_ids") or [],
        values=flow.get("inputs") or {},
        samples=flow.get("samples"),
        sets=flow.get("sets") or [],
        error=error,
        note=note,
    )


def _window(when) -> tuple[date, date]:
    today = timezone.localdate()
    if when is not None:
        return max(today, when.start_date), max(today, when.end_date)
    return today, today + timedelta(days=WINDOW_DAYS - 1)


def slots_reply(user, conversation, eq, *, when=None, note: str = "") -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import availability
    from iic_booking.research_copilot.services.assistant.dates import day_window, describe, when_from_payload

    flow = load(conversation)
    if int(flow.get("equipment_id") or 0) != int(eq.pk) or flow.get("samples") is None:
        return inputs_reply(user, conversation, eq, note="First tell me about your samples.")
    needed = availability.slots_needed(eq, flow.get("required_minutes"))
    start, end = _window(when)
    today = timezone.localdate()
    if when is None:
        window = when_from_payload({"start": start.isoformat(), "end": end.isoformat()})
    else:
        window = when
    lookup = availability._lookup(user, eq, start, end)
    if not lookup.ok:
        return C.reply(
            lookup.message or "Slot availability could not be loaded right now.",
            actions=[C.link("Open booking page", availability.booking_href(eq.pk, start)), C.flow_action("Cancel", "cancel")],
            intent="flow_slots",
        )
    rows = availability._filter_rows(lookup.rows, window)
    runs = availability._runs(rows, needed)
    days = availability._group_days(runs, window) if runs else []
    nearest = availability._nearest_days(user, eq, window, needed) if not days else []
    save(conversation, step="slots")

    span = (end - start).days + 1
    prev_start = max(today, start - timedelta(days=span))
    nav: list[dict[str, Any]] = []
    if start > today:
        nav.append({"label": "Earlier", "when": {"start": prev_start.isoformat(), "end": (prev_start + timedelta(days=span - 1)).isoformat()}})
    nxt = end + timedelta(days=1)
    nav.append({"label": "Later", "when": {"start": nxt.isoformat(), "end": (nxt + timedelta(days=span - 1)).isoformat()}})
    day_chips = []
    for i in range(min(span, 7)):
        d = start + timedelta(days=i)
        day_chips.append({"label": describe(day_window(d, today=today), today=today), "when": {"start": d.isoformat(), "end": d.isoformat()}})

    waitlist = int(getattr(eq, "waitlist_queue_depth", 0) or 0) > 0
    minutes = flow.get("required_minutes")
    lead = (note + "\n\n" if note else "")
    if minutes:
        lead += f"Your samples need about **{int(minutes)} minutes**"
        lead += f", so I'm showing {needed}-slot blocks." if needed > 1 else ", one slot."
        lead += " "
    if days:
        lead += f"Pick a start time for **{eq.name}** ({window.label})."
    else:
        lead += f"No free **{eq.name}** time fits for {window.label}."
        if nearest:
            lead += " Nearest days with room: " + ", ".join(f"{n['label']} ({n['count']})" for n in nearest) + "."
        if waitlist:
            lead += " The booking page can also put you on the waitlist."
        lead += " For an urgent run, raise an urgent request from the booking page."
    href = availability.booking_href(eq.pk, (days[0]["date"] if days else start))
    card = {
        "type": "ba_slots",
        "flow": True,
        "step": C.step_info(4, "Slot"),
        "equipment_id": int(eq.pk),
        "equipment_name": eq.name,
        "window_label": window.label,
        "when": window.to_payload(),
        "days": days,
        "estimate": None,
        "slots_needed": needed,
        "required_minutes": minutes,
        "can_book": True,
        "booking_intent": True,
        "nearest_days": nearest,
        "similar": [],
        "nav": nav,
        "day_chips": day_chips,
        "waitlist_href": href if waitlist and not days else None,
        "urgent_href": href + "&urgent=1" if not days else None,
        "booking_href": href,
    }
    return C.reply(
        lead.strip(),
        cards=[card],
        actions=[
            C.flow_action("Change samples/inputs", "edit_inputs", {"equipment_id": int(eq.pk)}),
            C.flow_action("Change equipment", "change_equipment"),
            C.flow_action("Cancel", "cancel"),
        ],
        intent="flow_slots",
        title_hint=f"{eq.name} booking",
        extra={"equipment_id": int(eq.pk)},
    )


def submit_inputs(user, conversation, eq, p: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import booking_flow

    samples = int(p.get("number_of_samples") or 1)
    checked = booking_flow.check_inputs(user, eq, samples, p.get("input_values") or {}, p.get("sample_sets") or [])
    save(
        conversation,
        equipment_id=int(eq.pk),
        department_id=int(eq.internal_department_id or 0),
        samples=checked["samples"],
        inputs=checked["provided"],
        sets=checked["sets_raw"],
    )
    if checked["errors"]:
        return inputs_reply(user, conversation, eq, error=checked["errors"])
    save(conversation, required_minutes=checked["required_minutes"])
    slot_ids = [int(x) for x in (p.get("slot_ids") or load(conversation).get("slot_ids") or [])]
    if slot_ids:
        out = booking_flow.review(
            user, eq, slot_ids, checked["samples"], checked["provided"], checked["sets_raw"], guided=True,
        )
        if (out.get("metadata") or {}).get("intent") != "assistant:book_slot_retry":
            return out
        return slots_reply(user, conversation, eq, note=out.get("content") or "")
    return slots_reply(user, conversation, eq)


def pick(user, conversation, eq, slot_ids: list[int]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import booking_flow

    flow = load(conversation)
    save(conversation, slot_ids=[int(x) for x in slot_ids])
    if int(flow.get("equipment_id") or 0) != int(eq.pk) or flow.get("samples") is None:
        return inputs_reply(user, conversation, eq, note="Tell me about your samples, then I'll prepare that time.")
    out = booking_flow.review(
        user, eq, slot_ids, int(flow.get("samples") or 1), flow.get("inputs") or {}, flow.get("sets") or [], guided=True,
    )
    if (out.get("metadata") or {}).get("intent") == "assistant:book_slot_retry":
        return slots_reply(user, conversation, eq, note=out.get("content") or "")
    return out


def cancel(conversation) -> dict[str, Any]:
    pid = ba_state.load(conversation).get("pending_proposal_id")
    if pid:
        try:
            from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store

            prop_store.invalidate_proposal(pid)
        except Exception:  # noqa: BLE001
            pass
    clear(conversation)
    return C.reply(
        "Booking cancelled; nothing was booked. Start again any time.",
        actions=[C.flow_action("Book equipment", "start", primary=True), C.link("Browse equipment", "/equipments")],
        intent="flow_cancelled",
    )


def handle(user, conversation, payload: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant.dates import when_from_payload

    step = payload.get("step")
    flow = load(conversation)
    if step == "cancel":
        return cancel(conversation)
    if step == "start":
        clear(conversation)
        return departments_reply(user, conversation)
    if step == "change_department":
        return departments_reply(user, conversation)
    if step == "department":
        return equipment_reply(user, conversation, payload.get("department_id"))
    if step == "change_equipment":
        if flow.get("department_id") is not None:
            return equipment_reply(user, conversation, flow.get("department_id"))
        return departments_reply(user, conversation)
    eq = _visible_equipment(user, payload.get("equipment_id") or flow.get("equipment_id"))
    if eq is None:
        return departments_reply(user, conversation, note="That equipment is no longer available to your account.")
    if step == "equipment":
        ba_state.remember_equipment(conversation, eq, None, "book")
        return inputs_reply(user, conversation, eq)
    if step == "edit_inputs":
        return inputs_reply(user, conversation, eq)
    if step == "inputs":
        return submit_inputs(user, conversation, eq, payload)
    if step in ("slots", "change_slot"):
        return slots_reply(user, conversation, eq, when=when_from_payload(payload.get("when")))
    if step == "slot":
        return pick(user, conversation, eq, payload.get("slot_ids") or [])
    return C.reply("That option is not available.", intent="invalid")


# --------------------------------------------------------------------------------- typed replies

def handle_text(user, conversation, text: str) -> dict[str, Any] | None:
    """Typed answers while the guided flow is waiting (department / instrument names, dates, cancel)."""
    from iic_booking.research_copilot.services.assistant import matching
    from iic_booking.research_copilot.services.assistant.dates import normalize, parse_when

    flow = load(conversation)
    step = flow.get("step")
    if not step:
        return None
    lower = normalize(text)
    if lower in ("cancel", "stop", "quit", "exit", "cancel booking", "never mind", "nevermind"):
        return cancel(conversation)
    if lower in ("back", "go back"):
        if step == "equipment":
            return departments_reply(user, conversation)
        if step in ("inputs", "slots"):
            return equipment_reply(user, conversation, flow.get("department_id"))
        return None
    if step == "department":
        eqs = bookable_equipment(user)
        for eq in eqs:
            dept = getattr(eq, "internal_department", None)
            names = {normalize(_dept_label(dept)), normalize(getattr(dept, "code", "") or "")} - {""}
            if lower in names:
                return equipment_reply(user, conversation, int(eq.internal_department_id or 0))
        return None
    if step == "equipment":
        m = matching.match_equipment(user, text)
        if m.status == "unique" and m.equipment is not None:
            ba_state.remember_equipment(conversation, m.equipment, None, "book")
            return inputs_reply(user, conversation, m.equipment)
        return None
    if step == "slots":
        when = parse_when(text)
        eq = _visible_equipment(user, flow.get("equipment_id"))
        if when.explicit and eq is not None and not when.past:
            return slots_reply(user, conversation, eq, when=when)
        return None
    return None
