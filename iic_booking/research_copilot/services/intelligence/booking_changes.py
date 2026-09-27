"""
Cancellation (entire / selected slots / reduced samples) and reschedule workflows.

Only the signed-in user's own bookings are listed. Partial refunds come from the portal
partial-cancel preview; the cancel itself runs the portal user-cancel endpoint through the
confirmation endpoint (never from chat text alone). Copilot never assumes entire vs partial.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.intelligence import messages as M
from iic_booking.research_copilot.services.intelligence import state as st
from iic_booking.research_copilot.services.intelligence.flows import (
    MAX_SLOT_CHOICES,
    Turn,
    _dt,
    _ids,
    contiguous_runs,
    lookup_slots,
    run_label,
    run_value,
)

MAX_BOOKINGS = 8
ACTIVE_STATUSES = ("PENDING", "BOOKED", "DISRUPTION_PENDING")


def _qs(user):
    from iic_booking.equipment.models import Booking, BookingStatus

    return (
        Booking.objects.select_related("equipment")
        .prefetch_related("daily_slots")
        .filter(
            user=user,
            status__in=[BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.DISRUPTION_PENDING],
            source_booking__isnull=True,
        )
    )


def _window(booking) -> tuple[bool, str | None]:
    """(self-service open, cutoff label) with the same rule as the portal user-cancel/reschedule views."""
    slots = sorted(booking.daily_slots.all(), key=lambda s: s.start_datetime or timezone.now())
    if not slots or slots[0].start_datetime is None:
        return False, None
    if getattr(booking, "maintenance_disruption_flag", False) or booking.status == "DISRUPTION_PENDING":
        return True, None
    hours = int(getattr(booking.equipment, "reschedule_hours_threshold", None) or 48)
    cutoff = slots[0].start_datetime - timedelta(hours=hours)
    local = timezone.localtime(cutoff)
    return timezone.now() <= cutoff, f"{local:%a %d %b, %H:%M}"


def _summary(booking) -> dict[str, Any]:
    slots = sorted(booking.daily_slots.all(), key=lambda s: s.start_datetime or timezone.now())
    start = timezone.localtime(slots[0].start_datetime) if slots and slots[0].start_datetime else None
    end = timezone.localtime(slots[-1].end_datetime) if slots and slots[-1].end_datetime else None
    open_, cutoff = _window(booking)
    label = f"#{booking.booking_id} {getattr(booking.equipment, 'name', '')}"
    if start:
        label += f" - {start:%a %d %b, %H:%M}" + (f"-{end:%H:%M}" if end else "")
    return {
        "booking_id": int(booking.booking_id),
        "equipment_id": int(booking.equipment_id) if booking.equipment_id else None,
        "equipment_name": getattr(booking.equipment, "name", ""),
        "status": booking.status,
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
        "slot_count": len(slots),
        "total_charge": str(booking.total_charge) if getattr(booking, "total_charge", None) is not None else None,
        "self_service_open": open_,
        "cutoff": cutoff,
        "label": label,
    }


def cancellable_bookings(user) -> list[dict[str, Any]]:
    now = timezone.now()
    out = []
    for b in _qs(user):
        slots = list(b.daily_slots.all())
        if not slots or not any(s.start_datetime and s.start_datetime > now for s in slots):
            continue
        out.append(_summary(b))
    out.sort(key=lambda r: r["start"] or "")
    return out[:MAX_BOOKINGS]


def _owned(user, booking_id):
    try:
        return _qs(user).filter(booking_id=int(booking_id)).first()
    except (TypeError, ValueError):
        return None


def _pick_booking(turn: Turn, *, workflow: str, kind: str, verb: str) -> dict[str, Any] | Any:
    """Owned booking from '#123' / 'next booking', else a CANCELLATION_SELECTION list. Returns a booking or a response."""
    ents = turn.ents
    if ents.booking_ref:
        b = _owned(turn.user, ents.booking_ref)
        if b is None:
            return M.error("I couldn't find that booking among your active bookings.", intent=turn.intent,
                           actions=[M.link("my_bookings", "My Bookings", "/my-bookings")])
        return b
    rows = cancellable_bookings(turn.user)
    if ents.next_booking and rows:
        return _owned(turn.user, rows[0]["booking_id"])
    if not rows:
        st.restart(turn.state)
        return turn.respond(message_type=M.TEXT, content=f"You have no upcoming bookings to {verb}.",
                            actions=[M.link("my_bookings", "My Bookings", "/my-bookings")], source_label=M.SOURCE_PORTAL)
    if len(rows) == 1:
        return _owned(turn.user, rows[0]["booking_id"])
    options = [{"value": str(r["booking_id"]), "label": r["label"], "match": r["equipment_name"]} for r in rows]
    turn.state["workflow"] = workflow
    turn.state["step"] = "choose_booking"
    st.set_choice(turn.state, kind=kind, prompt=f"Which booking would you like to {verb}?", options=options)
    lines = [f"**Which booking would you like to {verb}?**", ""] + [f"{i}. {o['label']}" for i, o in enumerate(options, 1)]
    return turn.respond(
        message_type=M.CANCELLATION_SELECTION,
        content="\n".join(lines),
        cards=[{"type": "booking_selection", "kind": kind, "items": rows}],
        actions=[M.choice(kind, o["value"], o["label"]) for o in options[:4]],
        source_label=M.SOURCE_PORTAL,
    )


def _closed(turn: Turn, info: dict[str, Any], verb: str) -> dict[str, Any]:
    st.restart(turn.state, booking_id=info["booking_id"])
    when = f" Self-service {verb} closed on {info['cutoff']}." if info.get("cutoff") else ""
    return turn.respond(
        message_type=M.TEXT,
        content=f"Booking #{info['booking_id']} ({info['equipment_name']}) is inside the equipment's {verb} window.{when} "
        "Only an admin can change it now. I can raise a support ticket for you.",
        actions=[M.ticket_action("user_requested", "Ask the admin (support ticket)"),
                 M.link("my_bookings", "My Bookings", f"/my-bookings?booking={info['booking_id']}")],
        source_label=M.SOURCE_PORTAL,
        extra={"booking_id": info["booking_id"]},
    )


# --------------------------------------------------------------------------- cancellation


def start_cancel(turn: Turn) -> dict[str, Any]:
    st.restart(turn.state, "cancel", partial_hint=turn.ents.partial or None, entire_hint=turn.ents.entire or None)
    picked = _pick_booking(turn, workflow="cancel", kind="cancel_booking", verb="cancel")
    if isinstance(picked, dict):
        return picked
    return cancel_selected(turn, picked)


def _partial_mode(booking) -> str:
    from iic_booking.equipment.booking_cancellation import partial_cancel_uses_input_reduction

    profile = (getattr(booking.equipment, "profile_type", "") or "").strip().upper()
    if profile == "PRINT_3D":
        return "print_items"
    if partial_cancel_uses_input_reduction(profile):
        return "input_reduction"
    return "slot_selection"


def cancel_selected(turn: Turn, booking) -> dict[str, Any]:
    info = _summary(booking)
    s = turn.state
    s["booking_id"] = info["booking_id"]
    s["workflow"] = "cancel"
    if not info["self_service_open"]:
        return _closed(turn, info, "cancellation")
    mode = _partial_mode(booking)
    s["partial_mode"] = mode
    options = [{"value": "entire", "label": "Cancel the entire booking"}]
    if mode == "slot_selection" and info["slot_count"] > 1:
        options.append({"value": "selected", "label": "Cancel selected slots only"})
    elif mode == "input_reduction" and _current_count(booking) > 1:
        options.append({"value": "reduce", "label": "Reduce the number of samples"})
    elif mode == "print_items":
        options.append({"value": "portal", "label": "Cancel some print files (on My Bookings)"})
    options.append({"value": "keep", "label": "Keep the booking"})
    if s.get("entire_hint") and not s.get("partial_hint"):
        return cancel_mode(turn, "entire")
    s["step"] = "cancel_mode"
    st.set_choice(s, kind="cancel_mode", prompt="Entire booking or part of it?", options=options)
    lines = [f"**{info['label']}**", "", f"- Slots: {info['slot_count']}"]
    if info["total_charge"]:
        lines.append(f"- Charged: {M.money(info['total_charge'])}")
    if info.get("cutoff"):
        lines.append(f"- Self-service cancellation open until {info['cutoff']}")
    lines += ["", "Do you want to cancel the entire booking or only part of it?"]
    return turn.respond(
        message_type=M.CANCELLATION_SELECTION,
        content="\n".join(lines),
        cards=[{"type": "cancel_mode", "booking": info, **M.choice_card("cancel_mode", "Entire or part?", options)}],
        actions=[M.choice("cancel_mode", o["value"], o["label"]) for o in options],
        source_label=M.SOURCE_PORTAL,
        extra={"booking_id": info["booking_id"]},
    )


def _current_count(booking) -> int:
    try:
        return int(float((booking.input_values or {}).get("A") or 0))
    except (TypeError, ValueError):
        return 0


def cancel_mode(turn: Turn, mode: str) -> dict[str, Any]:
    s = turn.state
    booking = _owned(turn.user, s.get("booking_id"))
    if booking is None:
        st.restart(s)
        return M.error("That booking is no longer active.", intent=turn.intent)
    info = _summary(booking)
    if mode == "keep":
        st.restart(s)
        return turn.respond(message_type=M.TEXT, content=f"OK, booking #{info['booking_id']} stays as it is.",
                            source_label=M.SOURCE_COPILOT)
    if mode == "portal":
        st.restart(s)
        return turn.respond(message_type=M.TEXT, content="Choose the print files to cancel on My Bookings.",
                            actions=[M.link("my_bookings", "Open My Bookings", f"/my-bookings?booking={info['booking_id']}&action=cancel")],
                            source_label=M.SOURCE_PORTAL)
    if mode == "entire":
        return _prepare_cancel(turn, booking)
    if mode == "selected":
        slots = sorted(booking.daily_slots.all(), key=lambda x: x.start_datetime or timezone.now())
        options = []
        for sl in slots:
            a, b = timezone.localtime(sl.start_datetime), timezone.localtime(sl.end_datetime) if sl.end_datetime else None
            options.append({"value": str(sl.pk), "label": f"{a:%a %d %b, %H:%M}" + (f"-{b:%H:%M}" if b else "")})
        s["step"] = "cancel_slots"
        st.set_choice(s, kind="cancel_slots", prompt="Select the slots to cancel", options=options)
        return turn.respond(
            message_type=M.CANCELLATION_SELECTION,
            content=f"Select the slots of booking #{info['booking_id']} you want to cancel, then press Continue. "
            "The other slots stay booked.",
            cards=[{"type": "choice_list", "kind": "cancel_slots", "prompt": "Slots to cancel", "multi": True,
                    "allow_text": False, "options": options, "submit_label": "Preview refund"}],
            actions=[],
            source_label=M.SOURCE_PORTAL,
            extra={"booking_id": info["booking_id"]},
        )
    if mode == "reduce":
        current = _current_count(booking)
        s["step"] = "cancel_reduce"
        s["current_count"] = current
        options = [{"value": str(n), "label": str(n)} for n in range(1, min(current, 6))]
        st.set_choice(s, kind="cancel_reduce", prompt="New number of samples", options=options)
        return turn.respond(
            message_type=M.FORM_REQUEST,
            content=f"Booking #{info['booking_id']} has {current} samples. How many samples do you want to keep?",
            cards=[{"type": "form_request", "submit_label": "Preview refund", "choice_kind": "cancel_reduce",
                    "fields": [{"key": "keep", "label": "Samples to keep", "type": "NUMERIC", "required": True, "min": 1,
                                "max": current - 1}]}],
            actions=[M.choice("cancel_reduce", o["value"], o["label"]) for o in options],
            source_label=M.SOURCE_PORTAL,
            extra={"booking_id": info["booking_id"]},
        )
    return M.error("Please choose how to cancel.", intent=turn.intent)


def cancel_slots_chosen(turn: Turn, value: str) -> dict[str, Any]:
    s = turn.state
    offered = {str(o.get("value")) for o in ((s.get("pending_choice") or {}).get("options") or [])}
    ids = _ids(value)
    if not ids or any(str(i) not in offered for i in ids):
        return M.error("Select slots from the list shown.", intent=turn.intent)
    booking = _owned(turn.user, s.get("booking_id"))
    if booking is None:
        st.restart(s)
        return M.error("That booking is no longer active.", intent=turn.intent)
    if len(ids) == len(offered):
        return _prepare_cancel(turn, booking)
    return _preview_and_prepare(turn, booking, body={"slot_ids": ids})


def cancel_reduce_chosen(turn: Turn, keep: int) -> dict[str, Any]:
    s = turn.state
    booking = _owned(turn.user, s.get("booking_id"))
    if booking is None:
        st.restart(s)
        return M.error("That booking is no longer active.", intent=turn.intent)
    current = _current_count(booking)
    if keep < 1 or keep >= current:
        return turn.respond(message_type=M.FORM_REQUEST,
                            content=f"Enter a number from 1 to {current - 1}, or choose Cancel the entire booking.",
                            source_label=M.SOURCE_PORTAL)
    reduced = dict(booking.input_values or {})
    reduced["A"] = str(keep)
    return _preview_and_prepare(turn, booking, body={"reduced_input_values": reduced})


def _preview_and_prepare(turn: Turn, booking, *, body: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.v2.mutations import domain_bridge

    code, preview = domain_bridge.call_partial_cancel_preview(user=turn.user, booking_id=int(booking.booking_id), body=body)
    if code >= 400:
        return M.error((preview or {}).get("error") or "The partial cancellation could not be previewed.", intent=turn.intent,
                       actions=[M.link("my_bookings", "Open My Bookings", f"/my-bookings?booking={booking.booking_id}&action=cancel")])
    return _prepare_cancel(turn, booking, slot_ids=body.get("slot_ids"), reduced=body.get("reduced_input_values"), preview=preview)


def _prepare_cancel(turn: Turn, booking, *, slot_ids=None, reduced=None, preview=None) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import actions_enabled
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
    from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
    from iic_booking.research_copilot.services.v2.orchestrator import _proposal_card, _store_context

    s = turn.state
    prep = booking_mut.prepare_cancellation(
        user=turn.user, booking_id=int(booking.booking_id), slot_ids=slot_ids, reduced_input_values=reduced, preview=preview
    )
    if not prep.get("ok"):
        return M.error(prep.get("message") or "The cancellation could not be prepared.", intent=turn.intent, offer_ticket=True)
    executable = bool(prep.get("executable")) and actions_enabled()
    info = _summary(booking)
    partial = prep.get("cancel_mode") != "entire"
    lines = [f"**{'Partial cancellation' if partial else 'Cancel booking'}: #{info['booking_id']}**", "",
             f"- Equipment: {info['equipment_name']}"]
    if info["start"]:
        lines.append(f"- Booking time: {run_label([{'start': info['start'], 'end': info['end'], 'date': ''}])}")
    if partial and preview:
        released = preview.get("slots_to_release") or []
        if released:
            lines.append("- Slots to cancel: " + ", ".join(
                run_label([{"start": r.get("start_datetime"), "end": r.get("end_datetime")}]) for r in released[:8]))
        lines.append(f"- Slots that stay booked: {preview.get('slots_to_keep_count')}")
        if preview.get("new_input_values", {}).get("A") is not None and prep.get("cancel_mode") == "reduce_inputs":
            lines.append(f"- Samples after the change: {preview['new_input_values']['A']}")
        lines.append(f"- Refund: {M.money(preview.get('refund_amount'))}")
        lines.append(f"- New charge for the remaining booking: {M.money(preview.get('new_charge'))}")
    else:
        if info["total_charge"]:
            lines.append(f"- Charged: {M.money(info['total_charge'])}")
        lines.append("- Refund: as per the portal cancellation policy for this booking")
    lines.append("")
    card = _proposal_card(prep) | {
        "message_type": M.CONFIRMATION,
        "executable": executable,
        "cancel_mode": prep.get("cancel_mode"),
        "refund_amount": (preview or {}).get("refund_amount"),
        "new_charge": (preview or {}).get("new_charge"),
        "slots_to_keep_count": (preview or {}).get("slots_to_keep_count"),
        "slots_to_release": (preview or {}).get("slots_to_release") or [],
    }
    actions: list[dict[str, Any]] = []
    s["step"] = "confirm"
    if executable:
        lines.append("Nothing changes until you press **Confirm Cancellation**.")
        actions.append({
            "id": "confirm_proposal",
            "label": "Confirm Cancellation",
            "prompt": "Confirm",
            "enabled": True,
            "requires_confirmation": True,
            "proposal_id": prep.get("proposal_id"),
            "confirmation_token": prep.get("confirmation_token"),
            "mutation_action": "CANCEL_BOOKING",
        })
        _store_context(turn.conversation, {"proposal_id": prep.get("proposal_id"),
                                           "confirmation_token": prep.get("confirmation_token"),
                                           "pending_action": "CANCEL_BOOKING", "booking_id": info["booking_id"]})
    else:
        prop_store.invalidate_proposal(prep.get("proposal_id"))
        card["proposal_id"] = None
        card["confirmation_token"] = None
        lines.append("Cancelling from Copilot is not enabled for your account yet. Use My Bookings to cancel.")
    st.set_choice(s, kind="cancel_mode", prompt="Change", options=[{"value": "keep", "label": "Keep the booking"}])
    actions.append(M.choice("cancel_mode", "keep", "Keep the booking"))
    actions.append(M.link("my_bookings", "Open My Bookings", prep.get("portal_href") or "/my-bookings"))
    return turn.respond(
        message_type=M.CONFIRMATION,
        content="\n".join(lines),
        cards=[card],
        actions=actions,
        source_label=M.SOURCE_PORTAL,
        extra={"booking_id": info["booking_id"], "pending_action": "CANCEL_BOOKING" if executable else None,
               "executable": executable},
    )


# --------------------------------------------------------------------------- reschedule


def start_reschedule(turn: Turn) -> dict[str, Any]:
    st.restart(turn.state, "reschedule", period=turn.ents.period, date_text=turn.text if turn.ents.has_date else None)
    picked = _pick_booking(turn, workflow="reschedule", kind="reschedule_booking", verb="reschedule")
    if isinstance(picked, dict):
        return picked
    return reschedule_selected(turn, picked)


def reschedule_selected(turn: Turn, booking) -> dict[str, Any]:
    info = _summary(booking)
    s = turn.state
    s["booking_id"] = info["booking_id"]
    s["workflow"] = "reschedule"
    if not info["self_service_open"]:
        return _closed(turn, info, "reschedule")
    eq = booking.equipment
    required = max(1, info["slot_count"])
    lookup, rows, label = lookup_slots(turn.user, eq, text=s.get("date_text") or "", period=s.get("period"))
    if not lookup.ok:
        return M.error(lookup.message or "Slot availability could not be loaded.", intent=turn.intent)
    runs = contiguous_runs(rows, required)
    if not runs:
        return turn.respond(
            message_type=M.SLOT_LIST,
            content=f"No free {required}-slot window for **{eq.name}** in the {label}. Try another date, or use My Bookings.",
            actions=[M.link("my_bookings", "Open My Bookings", f"/my-bookings?booking={info['booking_id']}")],
            source_label=M.SOURCE_PORTAL,
        )
    shown = runs[:MAX_SLOT_CHOICES]
    options = [{"value": run_value(r), "label": run_label(r)} for r in shown]
    s["step"] = "reschedule_slot"
    st.set_choice(s, kind="reschedule_slot", prompt="New time", options=options)
    lines = [f"**Reschedule #{info['booking_id']} ({eq.name})**", "", f"Current: {info['label'].split(' - ', 1)[-1]}",
             "", "New times with the same duration:"] + [f"{i}. {o['label']}" for i, o in enumerate(options, 1)]
    return turn.respond(
        message_type=M.SLOT_LIST,
        content="\n".join(lines),
        cards=[{"type": "slot_list", "equipment_name": eq.name, "choice_kind": "reschedule_slot", "window": label,
                "items": [{"slot_ids": _ids(o["value"]), "label": o["label"], "start": r[0]["start"], "end": r[-1]["end"],
                           "date": r[0]["date"]} for o, r in zip(options, shown)]}],
        actions=[M.choice("reschedule_slot", o["value"], o["label"]) for o in options[:3]],
        source_label=M.SOURCE_PORTAL,
        extra={"booking_id": info["booking_id"]},
    )


def reschedule_slot_chosen(turn: Turn, value: str) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import actions_enabled
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
    from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
    from iic_booking.research_copilot.services.v2.orchestrator import _proposal_card, _store_context

    s = turn.state
    booking = _owned(turn.user, s.get("booking_id"))
    if booking is None:
        st.restart(s)
        return M.error("That booking is no longer active.", intent=turn.intent)
    prep = booking_mut.prepare_reschedule(user=turn.user, booking_id=int(booking.booking_id), slot_ids=_ids(value))
    if not prep.get("ok") or prep.get("status") != "READY_FOR_CONFIRMATION":
        resp = reschedule_selected(turn, booking)
        resp["content"] = (prep.get("message") or "That time is no longer available.") + "\n\n" + resp["content"]
        return resp
    executable = bool(prep.get("executable")) and actions_enabled()
    start, end = _dt(prep.get("start_time")), _dt(prep.get("end_time"))
    info = _summary(booking)
    lines = [f"**Reschedule #{info['booking_id']}**", "", f"- Equipment: {info['equipment_name']}",
             f"- From: {info['label'].split(' - ', 1)[-1]}"]
    if start:
        lines.append(f"- To: {start:%a %d %b, %H:%M}" + (f"-{end:%H:%M}" if end else ""))
    lines += ["- Charges: unchanged unless the portal reschedule rules say otherwise", ""]
    card = _proposal_card(prep) | {"message_type": M.CONFIRMATION, "executable": executable}
    actions: list[dict[str, Any]] = []
    s["step"] = "confirm"
    if executable:
        lines.append("Nothing changes until you press **Confirm Reschedule**. Equipment-group rules are checked by the portal.")
        actions.append({
            "id": "confirm_proposal",
            "label": "Confirm Reschedule",
            "prompt": "Confirm",
            "enabled": True,
            "requires_confirmation": True,
            "proposal_id": prep.get("proposal_id"),
            "confirmation_token": prep.get("confirmation_token"),
            "mutation_action": "RESCHEDULE_BOOKING",
        })
        _store_context(turn.conversation, {"proposal_id": prep.get("proposal_id"),
                                           "confirmation_token": prep.get("confirmation_token"),
                                           "pending_action": "RESCHEDULE_BOOKING", "booking_id": info["booking_id"]})
    else:
        prop_store.invalidate_proposal(prep.get("proposal_id"))
        card["proposal_id"] = None
        card["confirmation_token"] = None
        lines.append("Rescheduling from Copilot is not enabled for your account yet. Use My Bookings.")
    actions.append(M.link("my_bookings", "Open My Bookings", f"/my-bookings?booking={info['booking_id']}"))
    return turn.respond(
        message_type=M.CONFIRMATION,
        content="\n".join(lines),
        cards=[card],
        actions=actions,
        source_label=M.SOURCE_PORTAL,
        extra={"booking_id": info["booking_id"], "pending_action": "RESCHEDULE_BOOKING" if executable else None,
               "executable": executable},
    )
