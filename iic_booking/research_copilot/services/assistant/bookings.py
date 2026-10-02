"""
The signed-in user's own bookings: lists, details by booking ID and per-booking next steps (never anyone else's).

Each booking row carries the action chips the portal would allow right now (`eligibility`). The chips are
`ba_booking` actions that are re-checked against ownership and the same rules when pressed; anything that
changes a booking goes through the existing cancel / reschedule proposal flow and its Confirm button.
"""

from __future__ import annotations

import re
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.assistant import cards as C

_ACTIVE_EXCLUDED = ("CANCELLED", "REFUNDED", "COMPLETED", "ABSENT")
_NUMERIC_REF = re.compile(r"(?:\bbooking\s*(?:id|no\.?|number|ref)?\s*[#:]?\s*|#\s*)(\d{1,9})\b", re.IGNORECASE)
ACTIVE_STATUSES = ("PENDING", "BOOKED", "DISRUPTION_PENDING")
RESULT_STATUSES = ("PROCESSING", "COMPLETED")
REBOOK_STATUSES = ("COMPLETED", "CANCELLED", "REFUNDED", "BOOKING_NOT_UTILIZED", "ABSENT", "OTHER_DISRUPTION")
MAX_LIST = 8
NEXT_PROMPT = "What would you like to do next?"

OP_LABELS = {
    "details": "View details",
    "cancel": "Cancel",
    "reschedule": "Reschedule",
    "edit": "Edit parameters",
    "message": "Message the lab",
    "results": "View results",
    "invoice": "Download invoice",
    "rate": "Rate experience",
    "rebook": "Book again",
    "template": "Save as template",
}


def booking_ref(text: str) -> str | None:
    from iic_booking.research_copilot.services.booking_refs import find_virtual_ref

    virtual = find_virtual_ref(text or "")
    if virtual:
        return virtual
    m = _NUMERIC_REF.search(text or "")
    return m.group(1) if m else None


def _base_qs(user):
    from iic_booking.equipment.models import Booking

    return Booking.objects.filter(user=user).select_related("equipment", "user").prefetch_related("daily_slots")


def owned(user, booking_id):
    try:
        return _base_qs(user).filter(pk=int(booking_id)).first()
    except (TypeError, ValueError):
        return None


def find_owned(user, ref: str):
    from django.db.models import Q

    filt = Q(virtual_booking_id__iexact=ref)
    if ref.isdigit():
        filt |= Q(booking_id=int(ref))
    return _base_qs(user).filter(filt).first()


def _slots(b) -> list:
    return sorted(b.daily_slots.all(), key=lambda s: s.start_datetime or timezone.now())


def _lab_message_state(b, status: str, now) -> tuple[bool, str]:
    try:
        from iic_booking.equipment.booking_lab_messages import lab_message_policy

        return lab_message_policy(b, now=now)
    except Exception:  # noqa: BLE001
        return status != "WAITLISTED", ""


def eligibility(b, *, now=None) -> dict[str, Any]:
    """Which next steps the portal allows for this booking right now (same rules as My Bookings)."""
    from iic_booking.research_copilot.services.intelligence.booking_changes import _window

    now = now or timezone.now()
    slots = _slots(b)
    future = any(s.start_datetime and s.start_datetime > now for s in slots)
    status = str(b.status or "")
    repeat = getattr(b, "source_booking_id", None) is not None
    active = status in ACTIVE_STATUSES and future and not repeat
    open_, cutoff = _window(b) if active else (False, None)
    eq = b.equipment
    results = False
    results_blocked = ""
    if status == "COMPLETED":
        try:
            from iic_booking.equipment.booking_results_service import has_material_result_files

            results = bool(has_material_result_files(b))
        except Exception:  # noqa: BLE001
            results = False
    if results:
        if getattr(eq, "user_rating_enabled", True) and (b.rating is None or getattr(b, "rating_removed", False)):
            results_blocked = "rating"
        elif getattr(b, "istem_fbr_status", None) and str(b.istem_fbr_status) != "EXECUTED":
            results_blocked = "istem_fbr"
    message, message_reason = _lab_message_state(b, status, now)
    reschedule_locked = False
    if active:
        from iic_booking.equipment.reschedule_lock import reschedule_locked_for

        reschedule_locked = reschedule_locked_for(getattr(b, "user", None), b)
    return {
        "active": active,
        "future": future,
        "self_service_open": bool(open_),
        "cutoff": cutoff,
        "cancel": active and bool(open_),
        "reschedule": active and bool(open_) and not reschedule_locked,
        "reschedule_locked": reschedule_locked,
        "edit": status == "BOOKED" and not repeat and bool(b.input_values),
        "message": message,
        "message_reason": message_reason,
        "results": results,
        "results_blocked": results_blocked,
        "invoice": status == "COMPLETED",
        "rate": status == "COMPLETED" and getattr(b, "rating", None) is None and bool(getattr(eq, "user_rating_enabled", False)),
        "rebook": status in REBOOK_STATUSES or (not future and status not in ACTIVE_STATUSES),
        "template": status in ("COMPLETED", "BOOKED", "PROCESSING") and bool(b.input_values),
    }


def _results_line(elig: dict[str, Any]) -> str:
    if not elig["results"]:
        return "not uploaded yet."
    if elig["results_blocked"] == "rating":
        return "uploaded — submit your rating first, then you can download them."
    if elig["results_blocked"] == "istem_fbr":
        return "uploaded — they unlock once your I-STEM FBR number is verified by the Officer In Charge."
    return "available — open the booking to download them."


def chip(b, op: str, *, primary: bool = False) -> dict[str, Any]:
    from iic_booking.research_copilot.services.booking_refs import display_ref

    ref = display_ref(b)
    return C.assistant_action(
        OP_LABELS[op], "ba_booking", {"booking_id": int(b.pk), "op": op}, primary=primary,
        utterance=f"{OP_LABELS[op]} {ref}",
    )


def chips_for(b, elig: dict[str, Any], *, include_details: bool = True, limit: int | None = None) -> list[dict[str, Any]]:
    order = ["details"] if include_details else []
    if elig["active"]:
        order += ["reschedule", "cancel", "edit", "message"]
    else:
        order += ["results", "invoice", "rate", "message", "edit", "rebook", "template"]
    ops = [op for op in order if op == "details" or elig.get(op)]
    out = [chip(b, op, primary=(i == 0 and op != "details")) for i, op in enumerate(ops)]
    return out[:limit] if limit else out


def _row(b, elig: dict[str, Any] | None = None, *, with_actions: bool = False) -> dict[str, Any]:
    from iic_booking.research_copilot.services.booking_refs import display_ref

    slots = _slots(b)
    start = slots[0].start_datetime if slots else None
    end = slots[-1].end_datetime if slots else None
    ls = timezone.localtime(start) if start else None
    le = timezone.localtime(end) if end else None
    row = {
        "booking_id": int(b.pk),
        "reference": display_ref(b),
        "equipment": getattr(b.equipment, "name", ""),
        "equipment_id": int(b.equipment_id) if b.equipment_id else None,
        "status": b.status,
        "status_label": b.get_status_display() if hasattr(b, "get_status_display") else b.status,
        "when": f"{ls:%a %d %b %Y, %H:%M}" + (f"–{le:%H:%M}" if le else "") if ls else "",
        "start": ls.isoformat() if ls else None,
        "charge": float(b.total_charge) if getattr(b, "total_charge", None) is not None else None,
        "href": f"/my-bookings?booking={b.pk}",
    }
    if with_actions:
        elig = elig or eligibility(b)
        row["actions"] = chips_for(b, elig, limit=4)
        row["self_service_open"] = elig["self_service_open"]
        if elig.get("cutoff"):
            row["cutoff"] = elig["cutoff"]
    return row


def _remember(conversation, rows: list[dict[str, Any]], *, focus: int | None = None) -> None:
    from iic_booking.research_copilot.services.assistant import state as ba_state

    values: dict[str, Any] = {"last_booking_ids": [r["booking_id"] for r in rows][:MAX_LIST]}
    if focus is not None:
        values["focus_booking_id"] = int(focus)
    elif len(rows) == 1:
        values["focus_booking_id"] = rows[0]["booking_id"]
    ba_state.save(conversation, **values)


# --------------------------------------------------------------------------- lists

_SCOPE_TITLES = {
    "upcoming": "Upcoming bookings",
    "past": "Past bookings",
    "recent": "Recent bookings",
    "all": "Bookings",
}


def _filtered(user, scope: str, statuses: list[str] | None, equipment_words: list[str] | None):
    from django.db.models import Max, Min, Q

    now = timezone.now()
    qs = _base_qs(user).annotate(first_start=Min("daily_slots__start_datetime"), last_end=Max("daily_slots__end_datetime"))
    if statuses:
        qs = qs.filter(status__in=statuses)
    if scope == "upcoming":
        qs = qs.filter(last_end__gte=now)
        if not statuses:
            qs = qs.exclude(status__in=_ACTIVE_EXCLUDED)
        qs = qs.order_by("first_start")
    elif scope == "past":
        qs = qs.filter(Q(last_end__lt=now) | Q(status__in=("COMPLETED", "CANCELLED", "REFUNDED"))).order_by("-first_start")
    else:
        qs = qs.order_by("-created_at") if hasattr(qs.model, "created_at") else qs.order_by("-pk")
    if equipment_words:
        q = Q()
        for w in equipment_words:
            q |= Q(equipment__name__icontains=w) | Q(equipment__code__icontains=w)
        narrowed = qs.filter(q)
        if narrowed.exists():
            return narrowed, True
    return qs, False


_LIST_STOP = {
    "my", "mine", "show", "list", "view", "see", "display", "check", "get", "give", "all", "recent", "latest", "last",
    "upcoming", "future", "next", "past", "previous", "old", "history", "pending", "completed", "cancelled", "canceled",
    "confirmed", "booked", "refunded", "processing", "waitlisted", "current", "active", "scheduled", "bookings",
    "booking", "reservations", "reservation", "sessions", "session", "slots", "slot", "please", "me", "the", "a", "an",
    "of", "for", "on", "in", "what", "are", "is", "do", "i", "have", "did", "book", "tell", "about", "status", "and",
    "can", "you", "your", "with", "this", "week", "month", "today", "earlier", "older", "finished", "done", "hold",
    "unpaid", "disrupted", "appointments", "appointment", "requests", "request", "now", "when", "any", "which", "where",
}


def list_reply(user, conversation, *, scope: str = "recent", statuses: list[str] | None = None, limit: int = MAX_LIST,
               text: str = "") -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant.intents import normalize

    words = [w for w in normalize(text).split() if len(w) >= 3 and w not in _LIST_STOP and not w.isdigit()]
    qs, by_equipment = _filtered(user, scope, statuses, words[:3])
    bookings = list(qs[:limit])
    title = _SCOPE_TITLES.get(scope, "Bookings")
    if statuses and len(statuses) == 1:
        title = f"{bookings[0].get_status_display() if bookings else statuses[0].title()} bookings"
    if by_equipment and bookings:
        title = f"{bookings[0].equipment.name} bookings" if len({b.equipment_id for b in bookings}) == 1 else title
    if not bookings:
        empty = {
            "upcoming": "You have no upcoming bookings.",
            "past": "You have no past bookings yet.",
        }.get(scope, "I couldn't find any bookings matching that.")
        if scope == "upcoming" and limit == 1:
            empty = "You have no upcoming bookings."
        return C.reply(
            empty + " Tell me the equipment and a day (for example \"XRD next Monday\") and I'll find free slots.",
            actions=[C.flow_action("Book equipment", "start", primary=True),
                     C.prompt_action("Recent bookings", "Show my recent bookings"),
                     C.link("Open My Bookings", "/my-bookings")],
            intent="bookings",
            title_hint=title,
        )
    rows = [_row(b, with_actions=True) for b in bookings]
    _remember(conversation, rows)
    actions = _list_actions(scope)
    if len(rows) == 1:
        r = rows[0]
        intro = (f"Your next booking is **{r['equipment']}** on {r['when']} ({r['reference']}, {r['status_label']})."
                 if scope == "upcoming" and limit == 1
                 else f"I found 1 booking: **{r['equipment']}** — {r['when']} ({r['reference']}, {r['status_label']}).")
        hint = "Use the buttons on the booking."
    else:
        what = {"upcoming": "upcoming", "past": "past", "recent": "most recent"}.get(scope, "")
        intro = f"Here are your {len(rows)} {what} bookings.".replace("  ", " ")
        hint = "Use a booking's buttons, or say something like \"cancel the second one\"."
    content = f"{intro}\n\n{NEXT_PROMPT} {hint}"
    return C.reply(
        content,
        cards=[{"type": "ba_bookings", "title": title, "items": rows, "prompt": NEXT_PROMPT}],
        actions=actions,
        intent="bookings",
        title_hint=title,
        extra={"next_prompt": NEXT_PROMPT},
    )


def _list_actions(scope: str) -> list[dict[str, Any]]:
    """List-level next steps; per-booking actions live on each booking in the card."""
    other = (C.prompt_action("Past bookings", "Show my past bookings") if scope == "upcoming"
             else C.prompt_action("Upcoming bookings", "Show my upcoming bookings"))
    return [C.flow_action("Book equipment", "start"), other, C.link("Open My Bookings", "/my-bookings")]


def upcoming_reply(user, *, limit: int = MAX_LIST, conversation=None) -> dict[str, Any]:
    return list_reply(user, conversation, scope="upcoming", limit=limit)


# --------------------------------------------------------------------------- details

def detail_reply(user, conversation, b) -> dict[str, Any]:
    elig = eligibility(b)
    r = _row(b, elig)
    lines = [f"**{r['reference']}** · {r['equipment']}", "", f"- Status: **{r['status_label']}**"]
    if r["when"]:
        lines.append(f"- Slot: {r['when']}")
    if r["charge"] is not None:
        lines.append(f"- Charge: ₹{r['charge']:,.2f}")
    samples = (b.input_values or {}).get("A") if isinstance(b.input_values, dict) else None
    if samples:
        lines.append(f"- Samples: {samples}")
    if elig["active"]:
        if elig["self_service_open"] and elig.get("reschedule_locked"):
            if elig.get("cutoff"):
                lines.append(f"- You can cancel until {elig['cutoff']}.")
            lines.append("- Reschedule not available — sample accepted by the lab. Use Message the lab to contact "
                         "the Officer in Charge.")
        elif elig["self_service_open"]:
            if elig.get("cutoff"):
                lines.append(f"- You can cancel or reschedule until {elig['cutoff']}.")
        else:
            lines.append("- The self-service cancel/reschedule window has closed; the lab or admin can still help "
                         "(use Message the lab or raise a support ticket).")
    if str(b.status) == "PROCESSING":
        lines.append("- Results: available after the analysis is completed.")
    elif str(b.status) == "COMPLETED":
        lines.append("- Results: " + _results_line(elig))
    lines += ["", NEXT_PROMPT]
    actions = chips_for(b, elig, include_details=False)
    if not elig["self_service_open"] and elig["active"]:
        from iic_booking.research_copilot.services.intelligence import messages as M

        actions.append(M.ticket_action("user_requested", "Ask the admin (support ticket)"))
    actions.append(C.link("Open in My Bookings", r["href"]))
    _remember(conversation, [r], focus=int(b.pk))
    return C.reply(
        "\n".join(lines),
        cards=[{"type": "ba_bookings", "title": "Booking details", "items": [r]}],
        actions=actions,
        intent="booking_details",
        title_hint=f"Booking {r['reference']}",
        extra={"booking_id": int(b.pk), "next_prompt": NEXT_PROMPT},
    )


def status_reply(user, ref: str, *, conversation=None) -> dict[str, Any]:
    b = find_owned(user, ref)
    if b is None:
        return C.reply(
            f"I couldn't find booking **{ref}** among your bookings. Check the ID in My Bookings.",
            actions=[C.prompt_action("Show my bookings", "Show my recent bookings"), C.link("My Bookings", "/my-bookings")],
            intent="booking_status",
        )
    return detail_reply(user, conversation, b)
