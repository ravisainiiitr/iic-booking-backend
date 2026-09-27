"""
Structured Copilot actions (conversational buttons).

A button is a user turn, not a link: clicking it sends `{type, payload}` back to the conversation and
the server continues the same workflow. Only the vocabulary below is accepted, each type accepts only
its own payload keys, and every id in a payload is re-resolved under the signed-in user by the
handler (equipment visibility, booking ownership), so an edited payload can never reach another
user's record or an arbitrary endpoint.

Each action also carries `prompt` (the equivalent typed message), so a client that does not know the
structured form still continues the conversation by sending that text.
"""

from __future__ import annotations

import re
from typing import Any

# Equipment / technique
VIEW_EQUIPMENT = "VIEW_EQUIPMENT"
SEARCH_EQUIPMENT = "SEARCH_EQUIPMENT"
BOOK_EQUIPMENT = "BOOK_EQUIPMENT"
CHECK_AVAILABILITY = "CHECK_AVAILABILITY"
ESTIMATE_COST = "ESTIMATE_COST"
LEARN_TECHNIQUE = "LEARN_TECHNIQUE"
FIND_RELATED_TECHNIQUES = "FIND_RELATED_TECHNIQUES"
# Bookings
VIEW_BOOKINGS = "VIEW_BOOKINGS"
BOOKING_DETAILS = "BOOKING_DETAILS"
RESCHEDULE_BOOKING = "RESCHEDULE_BOOKING"
CANCEL_BOOKING = "CANCEL_BOOKING"
PARTIAL_CANCEL_BOOKING = "PARTIAL_CANCEL_BOOKING"
# Wallet
VIEW_WALLET = "VIEW_WALLET"
VIEW_WALLET_BALANCE = "VIEW_WALLET_BALANCE"
RECHARGE_WALLET = "RECHARGE_WALLET"
VIEW_WALLET_TRANSACTIONS = "VIEW_WALLET_TRANSACTIONS"
VIEW_CREDIT_STATUS = "VIEW_CREDIT_STATUS"
REQUEST_WALLET_CREDIT = "REQUEST_WALLET_CREDIT"
# Results
VIEW_RESULTS = "VIEW_RESULTS"
VIEW_RESULT = "VIEW_RESULT"
SAMPLE_STATUS = "SAMPLE_STATUS"
# My Research
OPEN_MY_RESEARCH = "OPEN_MY_RESEARCH"
CREATE_WORKSPACE = "CREATE_WORKSPACE"
VIEW_RESEARCH_GROUP = "VIEW_RESEARCH_GROUP"
# Faculty / support
VIEW_AFFILIATIONS = "VIEW_AFFILIATIONS"
CREATE_SUPPORT_TICKET = "CREATE_SUPPORT_TICKET"
VIEW_SUPPORT_TICKETS = "VIEW_SUPPORT_TICKETS"
# General
ASK_CLARIFICATION = "ASK_CLARIFICATION"
PORTAL_HELP = "PORTAL_HELP"
SOMETHING_ELSE = "SOMETHING_ELSE"
START_OVER = "START_OVER"

_EQUIPMENT_KEYS = frozenset({"technique", "equipment_query", "equipment_id"})
_BOOKING_KEYS = frozenset({"booking_id"})
_NONE: frozenset[str] = frozenset()

PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    VIEW_EQUIPMENT: _EQUIPMENT_KEYS,
    SEARCH_EQUIPMENT: _EQUIPMENT_KEYS,
    BOOK_EQUIPMENT: _EQUIPMENT_KEYS,
    CHECK_AVAILABILITY: _EQUIPMENT_KEYS,
    ESTIMATE_COST: _EQUIPMENT_KEYS,
    LEARN_TECHNIQUE: _EQUIPMENT_KEYS,
    FIND_RELATED_TECHNIQUES: _EQUIPMENT_KEYS,
    VIEW_BOOKINGS: _NONE,
    BOOKING_DETAILS: _BOOKING_KEYS,
    RESCHEDULE_BOOKING: _BOOKING_KEYS,
    CANCEL_BOOKING: _BOOKING_KEYS,
    PARTIAL_CANCEL_BOOKING: _BOOKING_KEYS,
    VIEW_WALLET: _NONE,
    VIEW_WALLET_BALANCE: _NONE,
    RECHARGE_WALLET: _NONE,
    VIEW_WALLET_TRANSACTIONS: _NONE,
    VIEW_CREDIT_STATUS: _NONE,
    REQUEST_WALLET_CREDIT: _NONE,
    VIEW_RESULTS: _NONE,
    VIEW_RESULT: _BOOKING_KEYS,
    SAMPLE_STATUS: _BOOKING_KEYS,
    OPEN_MY_RESEARCH: _NONE,
    CREATE_WORKSPACE: _NONE,
    VIEW_RESEARCH_GROUP: _NONE,
    VIEW_AFFILIATIONS: _NONE,
    CREATE_SUPPORT_TICKET: _NONE,
    VIEW_SUPPORT_TICKETS: _NONE,
    ASK_CLARIFICATION: frozenset({"topic"}),
    PORTAL_HELP: frozenset({"topic"}),
    SOMETHING_ELSE: frozenset({"topic", "technique", "equipment_query"}),
    START_OVER: _NONE,
}
ACTION_TYPES = frozenset(PAYLOAD_KEYS)

TOPICS = frozenset(
    {
        "help", "equipment", "bookings", "wallet", "credit", "credit_settlement", "results", "my_research",
        "support", "faculty", "faculty_association", "portal",
    }
)

PRIMARY = "primary"
SECONDARY = "secondary"

_QUERY_RE = re.compile(r"^[\w\s\-+().,/&']{1,80}$")


class InvalidAction(ValueError):
    pass


def make(
    action_type: str,
    label: str,
    *,
    payload: dict[str, Any] | None = None,
    utterance: str | None = None,
    style: str = SECONDARY,
    confirmation_required: bool = False,
) -> dict[str, Any]:
    if action_type not in ACTION_TYPES:
        raise InvalidAction(action_type)
    clean = {k: v for k, v in (payload or {}).items() if v not in (None, "")}
    said = utterance or label
    suffix = ":".join(str(clean[k]).lower().replace(" ", "_") for k in sorted(clean))
    out: dict[str, Any] = {
        "id": f"act:{action_type.lower()}" + (f":{suffix}" if suffix else ""),
        "label": label,
        "action_type": action_type,
        "payload": clean,
        "utterance": said,
        "prompt": said,
        "style": style,
        "confirmation_required": confirmation_required,
        "enabled": True,
    }
    if style == PRIMARY:
        out["primary"] = True
    return out


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        raise InvalidAction("invalid_id")
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise InvalidAction("invalid_id") from None
    if n <= 0 or n > 2_147_483_647:
        raise InvalidAction("invalid_id")
    return n


def parse(raw: Any) -> dict[str, Any] | None:
    """Validate a client action. None when absent; InvalidAction when malformed or not allowed."""
    from iic_booking.research_copilot.services.intelligence import terminology

    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise InvalidAction("invalid_action")
    action_type = str(raw.get("type") or raw.get("action_type") or "").strip().upper()
    if action_type not in ACTION_TYPES:
        raise InvalidAction("unknown_action")
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict):
        raise InvalidAction("invalid_action_payload")
    allowed = PAYLOAD_KEYS[action_type]
    extra = set(payload) - allowed
    if extra:
        raise InvalidAction("invalid_action_payload")
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if value in (None, ""):
            continue
        if key in {"equipment_id", "booking_id"}:
            clean[key] = _positive_int(value)
        elif key == "technique":
            v = str(value).strip().lower()
            if v not in terminology.TECHNIQUES:
                raise InvalidAction("invalid_action_payload")
            clean[key] = v
        elif key == "topic":
            v = str(value).strip().lower()
            if v not in TOPICS:
                raise InvalidAction("invalid_action_payload")
            clean[key] = v
        elif key == "equipment_query":
            v = " ".join(str(value).split())
            if not _QUERY_RE.match(v):
                raise InvalidAction("invalid_action_payload")
            clean[key] = v
    return {"type": action_type, "payload": clean}
