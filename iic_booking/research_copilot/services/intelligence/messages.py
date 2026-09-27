"""
Structured Copilot message envelopes.

Every intelligence response carries `metadata.message_type` (TEXT, CHOICE_LIST, EQUIPMENT_LIST,
EQUIPMENT_CARD, SLOT_LIST, FORM_REQUEST, BOOKING_SUMMARY, CONFIRMATION, BOOKING_RESULT,
CANCELLATION_SELECTION, UPDATE_STATUS, KNOWLEDGE_ANSWER, SUPPORT_TICKET, ERROR) plus a
`source_label` so the UI can show where an answer came from. Choice buttons carry
`choice: {kind, value}`; the server only accepts values it offered in the conversation state.
"""

from __future__ import annotations

from typing import Any

from iic_booking.research_copilot.services.v2.response_builder import build_response

TEXT = "TEXT"
CHOICE_LIST = "CHOICE_LIST"
EQUIPMENT_LIST = "EQUIPMENT_LIST"
EQUIPMENT_CARD = "EQUIPMENT_CARD"
SLOT_LIST = "SLOT_LIST"
FORM_REQUEST = "FORM_REQUEST"
BOOKING_SUMMARY = "BOOKING_SUMMARY"
CONFIRMATION = "CONFIRMATION"
BOOKING_RESULT = "BOOKING_RESULT"
CANCELLATION_SELECTION = "CANCELLATION_SELECTION"
UPDATE_STATUS = "UPDATE_STATUS"
KNOWLEDGE_ANSWER = "KNOWLEDGE_ANSWER"
SUPPORT_TICKET = "SUPPORT_TICKET"
ERROR = "ERROR"

SOURCE_PORTAL = "Live portal data"
SOURCE_EQUIPMENT = "IIC equipment catalogue"
SOURCE_KNOWLEDGE = "Verified IIC answer"
SOURCE_TECHNIQUE = "General technique overview"
SOURCE_PRICING = "Portal charge engine"
SOURCE_SUPPORT = "IIC support system"
SOURCE_COPILOT = "Research Copilot"

_KIND_BY_TYPE = {
    TEXT: "ANSWER",
    CHOICE_LIST: "CLARIFICATION",
    EQUIPMENT_LIST: "LIVE_DATA",
    EQUIPMENT_CARD: "LIVE_DATA",
    SLOT_LIST: "LIVE_DATA",
    FORM_REQUEST: "CLARIFICATION",
    BOOKING_SUMMARY: "ACTION_PREPARATION",
    CONFIRMATION: "ACTION_PREPARATION",
    BOOKING_RESULT: "LIVE_DATA",
    CANCELLATION_SELECTION: "CLARIFICATION",
    UPDATE_STATUS: "LIVE_DATA",
    KNOWLEDGE_ANSWER: "ANSWER",
    SUPPORT_TICKET: "ACTION_REQUIRED",
    ERROR: "ERROR",
}

NO_VERIFIED_ANSWER_TEXT = "I don't have a verified answer for this yet."


def choice(kind: str, value: Any, label: str, *, description: str = "", primary: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": f"choice:{kind}:{value}",
        "label": label,
        "choice": {"kind": kind, "value": str(value)},
        "enabled": True,
    }
    if description:
        out["description"] = description
    if primary:
        out["primary"] = True
    return out


def prompt(action_id: str, label: str, text: str) -> dict[str, Any]:
    return {"id": action_id, "label": label, "prompt": text, "enabled": True}


def link(action_id: str, label: str, href: str) -> dict[str, Any]:
    return {"id": action_id, "label": label, "href": href, "enabled": True}


def ticket_action(reason: str = "no_verified_answer", label: str = "Raise Support Ticket") -> dict[str, Any]:
    return {"id": "raise_ticket", "label": label, "escalate": {"reason": reason}, "enabled": True}


def choice_card(kind: str, prompt_text: str, options: list[dict[str, Any]], *, multi: bool = False, allow_text: bool = True) -> dict[str, Any]:
    return {
        "type": "choice_list",
        "kind": kind,
        "prompt": prompt_text,
        "multi": multi,
        "allow_text": allow_text,
        "options": [
            {k: v for k, v in o.items() if k in {"value", "label", "description", "meta", "selected"}} for o in options
        ],
    }


def envelope(
    *,
    message_type: str,
    content: str,
    cards: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    source_label: str | None = SOURCE_COPILOT,
    intent: str = "",
    confidence: str = "",
    escalate: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "intelligence": True,
        "deterministic": True,
        "llm_used": False,
        "message_type": message_type,
        "source_label": source_label or "",
        "intent": intent,
        "intent_confidence": confidence,
    }
    if extra:
        meta.update(extra)
    return build_response(
        kind=_KIND_BY_TYPE.get(message_type, "ANSWER"),
        content=content,
        cards=cards,
        actions=actions,
        escalate=escalate,
        metadata=meta,
    )


def error(content: str, *, intent: str = "", actions: list[dict[str, Any]] | None = None, offer_ticket: bool = False, reason: str = "action_failed") -> dict[str, Any]:
    acts = list(actions or [])
    if offer_ticket:
        acts.append(ticket_action(reason))
    return envelope(message_type=ERROR, content=content, actions=acts, intent=intent, source_label=SOURCE_PORTAL)


def no_verified_answer(*, intent: str = "", suggestions: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    acts = [ticket_action("no_verified_answer")]
    acts.extend(suggestions or [])
    return envelope(
        message_type=SUPPORT_TICKET,
        content=NO_VERIFIED_ANSWER_TEXT + " I can raise a support ticket so the IIC team can help you.",
        cards=[{"type": "support_offer", "reason": "no_verified_answer"}],
        actions=acts,
        intent=intent,
        source_label=SOURCE_SUPPORT,
        escalate=True,
        extra={"no_verified_answer": True},
    )


def money(value: Any) -> str:
    if value is None:
        return "not available"
    try:
        return f"\u20b9{float(value):,.2f}"
    except (TypeError, ValueError):
        return str(value)
