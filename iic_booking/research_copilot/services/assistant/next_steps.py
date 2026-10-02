"""
Next-step chips after every answer and the role-based starter chips for the welcome message.

`ensure_next_steps` tops a reply up to three chips (equipment-specific when the reply is about one
instrument, otherwise "See my bookings" / "Book equipment" / "Check charges"), skipping replies that are
mid-flow choices or confirm-gated proposals where extra buttons would distract.
"""

from __future__ import annotations

from typing import Any

from iic_booking.research_copilot.services.assistant import cards as C

TARGET = 3
_CHOICE_CARDS = {
    "ba_equipment_options", "ba_flow_departments", "ba_flow_equipment", "ba_booking_form", "ba_booking_handoff",
    "ba_booking_summary", "choice_list", "proposal", "support_offer",
}


def _key(a: dict[str, Any]) -> tuple[str, str]:
    target = a.get("href") or a.get("prompt") or f"{a.get('action_type')}:{(a.get('payload') or {}).get('topic') or (a.get('payload') or {}).get('step') or ''}"
    return (str(a.get("label") or "").strip().lower(), str(target))


def _equipment_id(meta: dict[str, Any], cards: list[dict[str, Any]]) -> int | None:
    eid = meta.get("equipment_id")
    if not eid:
        for c in cards:
            if isinstance(c, dict) and c.get("equipment_id") and c.get("type") in ("ba_slots", "ba_equipment_info"):
                eid = c["equipment_id"]
                break
    try:
        return int(eid) if eid else None
    except (TypeError, ValueError):
        return None


def _generic(user) -> list[dict[str, Any]]:
    from iic_booking.research_copilot.services.assistant import daily

    if daily.is_staff(user):
        return daily.help_actions(user)
    return [
        C.prompt_action("See my bookings", "Show my upcoming bookings"),
        C.flow_action("Book equipment", "start"),
        C.prompt_action("Check charges", "What are the charges?"),
    ]


def _for_equipment(user, eid: int, intent: str) -> list[dict[str, Any]]:
    from django.utils import timezone

    from iic_booking.research_copilot.services.assistant.availability import _next7
    from iic_booking.research_copilot.services.assistant.engine import _visible

    eq = _visible(user, eid)
    if eq is None:
        return []
    out = []
    active = (eq.status or "").strip() == "ACTIVE"
    if active:
        out.append(C.flow_action("Book this equipment", "equipment", {"equipment_id": int(eq.pk)}))
    if intent != "assistant:info_charges":
        out.append(C.assistant_action("Check charges", "ba_info", {"equipment_id": int(eq.pk), "topic": "charges"},
                                      utterance=f"Charges for {eq.name}"))
    if active and not intent.startswith("assistant:availability"):
        out.append(C.assistant_action("Free slots this week", "ba_availability",
                                      {"equipment_id": int(eq.pk), "when": _next7(timezone.localdate())},
                                      utterance=f"Free {eq.name} slots this week"))
    return out


def ensure_next_steps(user, actions: list[dict[str, Any]] | None, *, metadata: dict[str, Any] | None = None,
                      cards: list[dict[str, Any]] | None = None, response_kind: str = "") -> list[dict[str, Any]]:
    actions = list(actions or [])
    if len(actions) >= TARGET or user is None or not getattr(user, "is_authenticated", False):
        return actions
    meta = metadata or {}
    cards = [c for c in (cards or []) if isinstance(c, dict)]
    if meta.get("executable") or meta.get("proposal_id") or meta.get("choices") or meta.get("pending_choice"):
        return actions
    if any(c.get("type") in _CHOICE_CARDS for c in cards) or response_kind in ("PROPOSAL", "CONFIRMATION"):
        return actions
    intent = str(meta.get("intent") or "")
    eid = _equipment_id(meta, cards)
    try:
        extra = (_for_equipment(user, eid, intent) if eid else []) or _generic(user)
    except Exception:  # noqa: BLE001
        return actions
    seen = {_key(a) for a in actions}
    labels = {k[0] for k in seen}
    for a in extra:
        if len(actions) >= TARGET:
            break
        k = _key(a)
        if k in seen or k[0] in labels:
            continue
        actions.append(a)
        seen.add(k)
        labels.add(k[0])
    return actions


def starter_actions(user) -> list[dict[str, Any]]:
    """4–6 most useful first questions for the user's role (welcome message chips)."""
    from iic_booking.research_copilot.services.assistant import daily

    t = daily.user_type(user)
    if t in {"manager", "operator"}:
        return daily.help_actions(user) + [C.prompt_action("Booking help", "help")]
    if t in {"admin", "dept_admin"} or getattr(user, "is_superuser", False):
        return daily.help_actions(user) + [C.flow_action("Book equipment", "start")]
    out = [
        C.flow_action("Book equipment", "start", primary=True),
        C.prompt_action("My upcoming bookings", "Show my upcoming bookings"),
        C.prompt_action("How much will it cost?", "How much do 5 XRD samples cost?"),
    ]
    if t in daily.STUDENT_TYPES:
        out += [C.prompt_action("Link supervisor's wallet", "How do I link my supervisor's wallet?"),
                C.prompt_action("Where are my results?", "Where are my results and data files?"),
                C.prompt_action("What should I prepare?", "What should I prepare before my booking?")]
    elif t == "faculty":
        out += [C.prompt_action("Wallet balance", "What is my wallet balance?"),
                C.prompt_action("My students", "Manage my students and spending limits"),
                C.prompt_action("Where are my results?", "Where are my results and data files?")]
    elif t in daily.EXTERNAL_TYPES:
        out += [C.prompt_action("My invoices", "Where are my invoices?"),
                C.prompt_action("Where are my results?", "Where are my results and data files?"),
                C.prompt_action("What should I prepare?", "What should I prepare before my booking?")]
    else:
        out += [C.prompt_action("Wallet balance", "What is my wallet balance?"),
                C.prompt_action("Where are my results?", "Where are my results and data files?")]
    return out[:6]
