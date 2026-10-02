"""
Booking Assistant turn handling.

Runs first in `send_message` for signed-in users. Day-to-day questions (my bookings, wallet, recharge,
results, invoices, waitlist, tickets, staff queues, ...) are answered by the deterministic intent table in
`intents` / `daily` with next-step chips. It also answers availability ("I need FESEM tomorrow — what
are my options?"), equipment and policy questions, "which equipment can do X", and drives booking through
clickable cards. Anything it does not recognise returns None so the existing intelligence, deterministic
and RAG layers answer as before.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from iic_booking.research_copilot.services.assistant import actions as A
from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant import matching
from iic_booking.research_copilot.services.assistant import state as ba_state
from iic_booking.research_copilot.services.assistant.dates import When, normalize, parse_when, strip_when, when_from_payload

logger = logging.getLogger(__name__)

_AVAIL_RE = re.compile(
    r"\b(availab\w*|free|open|vacant|slots?|options?|when can i|earliest|next available|any time|book\w*|reserve|"
    r"schedule|need|want|get a slot)\b"
)
# Portal/self-service topics handled by the existing layers (wallet, credit, tickets, Remote Analysis, ...).
_PORTAL_RE = re.compile(
    r"\b(wallet|credit|invoice|tickets?|password|profile|account|remote analysis|dsa|raa|transactions?|payments?|"
    r"supervisor|research group|my research|results?|sample status)\b"
)
_MY_BOOKINGS_RE = re.compile(r"\bmy\s+(\w+\s+)?bookings?\b")
_ESTIMATE_RE = re.compile(r"\bestimat\w*\b")
_HOW_TO_RE = re.compile(r"^(how|what)\s+(do|can|should|to|is the (process|procedure|way))\b|\bhow\s+to\b")
_BOOK_RE = re.compile(r"\b(book\w*|reserve|reservation|slots?|availab\w*)\b")
_QUESTION_RE = re.compile(r"^(how|what|when|can|could|is|are|do|does|will|until|why|where|which|who)\b|\?|\b(policy|rules?)\b")
_REQUEST_RE = re.compile(r"\b(can you|could you|please|pls|for me)\b")
_UPCOMING_RE = re.compile(
    r"\b(upcoming|future|scheduled)\s+(bookings?|slots?|sessions?)\b|\bmy\s+(upcoming|future)\b|"
    r"\bwhat\s+(have i|did i)\s+book\w*\b|\bmy\s+bookings?\s+(this|next)\s+week\b"
)
_STATUS_RE = re.compile(r"\b(status|track|where is|what happened|update on|progress|state of)\b")
_CHANGE_RE = re.compile(r"\b(cancel\w*|reschedul\w*|move|shift|refund)\b")
_CAPABILITY_RE = re.compile(
    r"\b(which|what)\s+(equipment|instruments?|machines?|facilit\w+|tools?|techniques?)\b|"
    r"\b(equipment|instruments?|facility)\s+(for|to)\s+\w+|\bwho\s+(offers|provides|does)\b|"
    r"\b(can|could)\s+(measure|analy[sz]e|detect|characteri[sz]e|determine)\b"
)
_INFO_TOPICS: tuple[tuple[str, re.Pattern], ...] = (
    ("location", re.compile(r"\b(where\s+is|where's|location|located|address|which\s+(room|building|lab)|how\s+to\s+reach|directions?)\b")),
    ("contacts", re.compile(r"\b(operators?|oic|officer\s+in\s+charge|in-?charge|contacts?|who\s+(runs|handles|manages|operates|is\s+responsible)|phone|e-?mail)\b")),
    ("charges", re.compile(r"\b(charges?|price|pricing|rates?|fees?|tariff|how\s+much)\b")),
    ("inputs", re.compile(r"\b(input\s+fields?|form\s+fields?|booking\s+form|what\s+(details|information|inputs)\b|parameters\s+(needed|required))")),
    ("instructions", re.compile(r"\b(sample\s+requirements?|requirements?|instructions?|guidelines?|sample\s+(size|amount|quantity|form|type)s?|what\s+(sample|should\s+i\s+(bring|send)))\b")),
    ("rules", re.compile(r"\b(booking\s+rules?|rules?|slot\s+(length|duration|size)|how\s+long\s+is\s+a\s+slot|cancellation\s+window)\b")),
    ("overview", re.compile(r"\b(what\s+is|what's|what\s+does|tell\s+me\s+about|used\s+for|details\s+(of|about|on)|info(rmation)?\s+(on|about)|specs|specifications|capabilit\w+|overview)\b")),
)
_MANUAL_RE = re.compile(r"\b(prepar\w*|procedure|protocol|how\s+(do|to|should)\s+i\s+(mount|coat|load|prepare)|troubleshoot\w*|manual|sop)\b")


def assistant_enabled() -> bool:
    from django.conf import settings

    return bool(getattr(settings, "BOOKING_ASSISTANT_ENABLED", True))


def _visible(user, equipment_id: int):
    from iic_booking.research_copilot.services.v2.equipment_resolver import _qs_visible

    try:
        return (
            _qs_visible(user)
            .select_related("category", "internal_department")
            .filter(pk=int(equipment_id))
            .first()
        )
    except (TypeError, ValueError):
        return None


def _context_equipment(user, conversation, ctx: dict[str, Any]):
    eid = ctx.get("equipment_id")
    if not eid:
        try:
            from iic_booking.research_copilot.services.intelligence import state as intel_state

            eid = intel_state.load(conversation).get("last_equipment_id")
        except Exception:  # noqa: BLE001
            eid = None
    return _visible(user, eid) if eid else None


def _gone() -> dict[str, Any]:
    return C.reply(
        "That equipment is no longer available to your account. Ask again and I'll look it up.",
        actions=[C.link("Browse equipment", "/equipments")],
        intent="expired",
    )


def _options_reply(m: matching.Match, *, intent: str, when: When | None, text_query: str) -> dict[str, Any]:
    rows = [matching.option_row(c) for c in m.candidates]
    q = text_query.strip()
    techs = matching.techniques_in(q)
    pretty = q.upper() if techs and len(q) <= 6 else q
    if m.status == "unavailable":
        names = ", ".join(f"**{r['name']}** ({r['status_label']})" for r in rows[:4])
        return C.reply(
            f"The equipment matching \"{pretty}\" can't be booked right now: {names}.",
            actions=[C.link("Browse equipment", "/equipments")],
            intent=intent,
        )
    if m.misspelled:
        prompt = f"I couldn't find \"{q}\" exactly. Did you mean one of these?"
    elif len(rows) == 1:
        prompt = f"Is this the \"{pretty}\" you mean?"
    else:
        prompt = f"Several instruments match \"{pretty}\". Which one do you mean?"
    title = {"availability": f"{pretty} availability", "book": f"{pretty} booking"}.get(intent, pretty)
    return C.reply(
        prompt,
        cards=[C.equipment_options_card(rows, title="Choose equipment", intent=intent, when=when, prompt=prompt, query=q)],
        intent=f"{intent}_choose",
        title_hint=title,
    )


def _not_found(query: str) -> dict[str, Any]:
    return C.reply(
        f"I couldn't find any equipment matching \"{query}\" that your account can book. "
        "Try the instrument name or technique (for example FESEM, XRD, TEM, ICP-MS), or browse the catalogue.",
        actions=[C.link("Browse equipment", "/equipments")],
        intent="not_found",
    )


def _for_equipment(user, conversation, eq, intent: str, when: When | None, topic: str | None = None) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import availability, info

    if intent in ("availability", "book"):
        when = when or parse_when("")
        out = availability.availability_reply(user, eq, when, booking_intent=intent == "book")
        ba_state.remember_equipment(conversation, eq, when, "availability")
        return out
    topic = topic or (intent if intent in A.TOPICS else "overview")
    out = info.equipment_info_reply(user, eq, topic)
    ba_state.remember_equipment(conversation, eq, None, "info")
    return out


def _remember_flow(conversation, eq, **values: Any) -> None:
    from iic_booking.research_copilot.services.assistant import guided

    flow = guided.load(conversation)
    fresh = {} if int(flow.get("equipment_id") or 0) == int(eq.pk) else {"samples": None, "inputs": {}, "sets": [], "required_minutes": None}
    guided.save(conversation, equipment_id=int(eq.pk), department_id=int(eq.internal_department_id or 0), **fresh, **values)


def _dispatch_action(user, conversation, action: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import booking_flow, bookings, guided

    t = action["type"]
    p = action.get("payload") or {}
    if t == A.UPCOMING:
        return bookings.upcoming_reply(user, conversation=conversation)
    if t == A.BOOKING:
        from iic_booking.research_copilot.services.assistant import daily

        return daily.dispatch_booking_action(user, conversation, p)
    if t == A.FLOW:
        return guided.handle(user, conversation, p)
    eq = _visible(user, p.get("equipment_id"))
    if eq is None:
        return _gone()
    if t == A.PICK_EQUIPMENT:
        intent = p.get("intent") or "availability"
        return _for_equipment(user, conversation, eq, intent, when_from_payload(p.get("when")))
    if t == A.AVAILABILITY:
        return _for_equipment(user, conversation, eq, "availability", when_from_payload(p.get("when")))
    if t == A.INFO:
        return _for_equipment(user, conversation, eq, "info", None, p.get("topic") or "overview")
    if t == A.PICK_SLOT:
        ba_state.remember_equipment(conversation, eq, None, "book")
        _remember_flow(conversation, eq, step="inputs", slot_ids=list(p["slot_ids"]))
        return booking_flow.pick_slot(user, eq, p["slot_ids"])
    if t == A.REVIEW:
        _remember_flow(
            conversation, eq, step="inputs", slot_ids=list(p["slot_ids"]),
            samples=int(p.get("number_of_samples") or 1), inputs=p.get("input_values") or {}, sets=p.get("sample_sets") or [],
        )
        return booking_flow.review(
            user, eq, p["slot_ids"], int(p.get("number_of_samples") or 1), p.get("input_values") or {}, p.get("sample_sets") or [],
        )
    return C.reply("That option is not available.", intent="invalid")


_START_BOOK_RE = re.compile(
    r"^(hi |hello |hey )?((i|we)\s+(want|would like|wish|need)\s+to\s+|(can|could)\s+(i|you)\s+(help\s+me\s+)?|"
    r"help\s+me\s+|let'?s\s+|please\s+)?"
    r"(book|make\s+a\s+booking|new\s+booking|start\s+a\s+booking|reserve)"
    r"(\s+(an?|some|the)?\s*(equipment|instruments?|machines?|slots?|booking|facility))?"
    r"(\s+(for\s+me|please|now))?[\s.!?]*$"
)


def _start_choice(user, conversation, choice: dict[str, Any] | None, action: dict[str, Any] | None) -> dict[str, Any] | None:
    """The 'Book equipment' quick action / BOOK_EQUIPMENT button start the guided flow here."""
    from iic_booking.research_copilot.services.assistant import guided

    if choice and choice.get("kind") == "start" and choice.get("value") == "book":
        return guided.handle(user, conversation, {"step": "start"})
    if action and str(action.get("type") or "").upper() == "BOOK_EQUIPMENT":
        p = action.get("payload") or {}
        eid = p.get("equipment_id")
        if not eid:
            query = str(p.get("equipment_query") or p.get("technique") or "").strip()
            if query:
                m = matching.match_equipment(user, query)
                if m.status == "unique" and m.equipment is not None:
                    eid = m.equipment.pk
        if eid and _visible(user, eid) is not None:
            return guided.handle(user, conversation, {"step": "equipment", "equipment_id": int(eid)})
        return guided.handle(user, conversation, {"step": "start"})
    return None


def _info_topic(lower: str) -> str | None:
    for topic, rx in _INFO_TOPICS:
        if rx.search(lower):
            return topic
    return None


def _classify(user, conversation, text: str, ctx: dict[str, Any], intel_busy: bool) -> dict[str, Any] | None:
    from iic_booking.research_copilot.services.assistant import bookings, info

    lower = normalize(text)
    if not lower or len(lower) < 3:
        return None
    is_question = bool(_QUESTION_RE.search(lower))
    is_request = bool(_REQUEST_RE.search(lower))

    ref = bookings.booking_ref(text)
    if ref and _STATUS_RE.search(lower) and not _CHANGE_RE.search(lower):
        return bookings.status_reply(user, ref, conversation=conversation)
    if _UPCOMING_RE.search(lower) and not _CHANGE_RE.search(lower):
        return bookings.upcoming_reply(user, conversation=conversation)

    when = parse_when(text)
    phrase = matching.equipment_phrase(strip_when(text, when))
    m = matching.match_equipment(user, phrase) if phrase else None

    policy_topic = info.detect_policy_topic(lower)
    if policy_topic and is_question and not is_request:
        eq = m.equipment if m and m.status == "unique" else None
        return info.policy_reply(user, policy_topic, text, eq)
    if policy_topic in ("cancel", "edit", "refund") or _CHANGE_RE.search(lower):
        return None
    if _PORTAL_RE.search(lower) or _MY_BOOKINGS_RE.search(lower) or _ESTIMATE_RE.search(lower):
        return None

    if _CAPABILITY_RE.search(lower) and not when.explicit:
        out = info.capability_reply(user, text)
        if out is not None:
            return out

    if _MANUAL_RE.search(lower):
        return None
    topic = _info_topic(lower)
    if topic == "charges" and re.search(r"\bestimat\w*\b|\b\d+\s+samples?\b", lower):
        topic = None
    wants_slots = bool(_AVAIL_RE.search(lower)) or when.explicit
    if topic and not (wants_slots and topic == "overview" and when.explicit):
        intent = topic
    elif wants_slots:
        intent = "book" if re.search(r"\b(book\w*|reserve)\b", lower) else "availability"
    else:
        intent = ""

    if not intent:
        return _planned(user, conversation, text, ctx) if phrase else None

    if _HOW_TO_RE.search(lower) and not (m and m.status in ("unique", "options")):
        return None
    if m is None:
        if intel_busy:
            return None
        if intent in ("availability", "book") and (when.explicit or re.search(r"\b(availab\w*|slots?|free)\b", lower)):
            eq = _context_equipment(user, conversation, ctx)
            if eq is not None:
                return _for_equipment(user, conversation, eq, intent, when)
        return None
    if m.status == "unique":
        return _for_equipment(user, conversation, m.equipment, intent, when if intent in ("availability", "book") else None)
    if m.status in ("options", "unavailable"):
        return _options_reply(m, intent="availability" if intent == "book" else intent, when=when if when.explicit else None, text_query=phrase)
    if matching.techniques_in(phrase) or not _BOOK_RE.search(lower) or len(phrase.split()) > 3:
        return None
    return _not_found(phrase)


def _planned(user, conversation, text: str, ctx: dict[str, Any]) -> dict[str, Any] | None:
    from iic_booking.research_copilot.services.assistant import bookings, info
    from iic_booking.research_copilot.services.assistant.planner import plan

    p = plan(text)
    if not p:
        return None
    intent = p["intent"]
    if intent == "upcoming":
        return bookings.upcoming_reply(user, conversation=conversation)
    if intent == "booking_status":
        ref = bookings.booking_ref(text)
        return bookings.status_reply(user, ref, conversation=conversation) if ref else None
    if intent == "capability":
        return info.capability_reply(user, text)
    if intent == "policy":
        topic = info.detect_policy_topic(normalize(text))
        return info.policy_reply(user, topic, text) if topic else None
    if not p["equipment"]:
        return None
    m = matching.match_equipment(user, p["equipment"])
    when = parse_when(p["when"]) if p["when"] else None
    mapped = "availability" if intent == "availability" else (p.get("topic") or "overview")
    if m.status == "unique":
        return _for_equipment(user, conversation, m.equipment, mapped, when)
    if m.status in ("options", "unavailable"):
        return _options_reply(m, intent=mapped, when=when, text_query=p["equipment"])
    return None


def _typed_confirm_reply(conversation, text: str) -> dict[str, Any] | None:
    """A typed "yes/confirm" after a booking summary never books; point at the Confirm booking button."""
    pid = ba_state.load(conversation).get("pending_proposal_id")
    if not pid:
        return None
    from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
    from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store

    if resolve_intent(text).intent != "confirm_proposal":
        return None
    if not prop_store.get_proposal(pid):
        return None
    return C.reply(
        "To place the booking, press **Confirm booking** on the booking summary card above. "
        "Typed replies never create a booking.",
        kind="CLARIFICATION",
        intent="typed_confirm",
        extra={"typed_confirm_blocked": True},
    )


def resolve_confirm(text: str) -> bool:
    from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent

    try:
        return resolve_intent(text).intent == "confirm_proposal"
    except Exception:  # noqa: BLE001
        return False


_CHANGE_CHOICE_KINDS = {"cancel_booking", "cancel_mode", "cancel_slots", "cancel_reduce", "reschedule_booking", "reschedule_slot"}


def _change_choice(user, conversation, choice: dict[str, Any] | None) -> dict[str, Any] | None:
    """Cancel / reschedule buttons started from a booking chip keep working when the intelligence layer is off."""
    from iic_booking.research_copilot.services.intelligence import intelligence_enabled

    kind = str((choice or {}).get("kind") or "")
    if kind not in _CHANGE_CHOICE_KINDS or intelligence_enabled():
        return None
    from iic_booking.research_copilot.services.intelligence import engine as intel_engine
    from iic_booking.research_copilot.services.intelligence import messages as M
    from iic_booking.research_copilot.services.intelligence import state as intel_state

    value = str(choice.get("value") or "")
    state = intel_state.load(conversation)
    if kind == "cancel_slots":
        valid = (state.get("pending_choice") or {}).get("kind") == kind
    else:
        valid = intel_state.match_choice(state, value=value, kind=kind) is not None
    if not valid:
        return M.error("That option has expired. Please ask again or pick from the latest options.", intent="CHOICE_EXPIRED")
    from iic_booking.research_copilot.services.assistant.daily import _intel_turn

    turn = _intel_turn(user, conversation, value, state.get("last_intent") or "")
    turn.state = state
    out = intel_engine.handle_choice(turn, kind, value)
    intel_state.save(conversation, turn.state)
    return out


def _daily(user, conversation, text: str, intel_state_dict: dict[str, Any]) -> dict[str, Any] | None:
    from iic_booking.research_copilot.services.assistant import daily, intents

    pending = intel_state_dict.get("pending_choice")
    if pending:
        from iic_booking.research_copilot.services.intelligence import state as intel_state

        if intel_state.match_choice(intel_state_dict, text=text) is not None:
            return None
    det = intents.detect(text)
    if det is None or det.intent in ("howto_cancel", "howto_reschedule"):
        return None
    if pending and det.intent in ("booking_details",) and det.params.get("ordinal") is not None:
        return None
    out = daily.handle(user, conversation, det, text)
    if out is not None:
        out.setdefault("metadata", {})["daily_intent"] = det.intent
    return out


def try_assistant_turn(
    *,
    user,
    text: str,
    conversation,
    assistant_action: dict[str, Any] | None = None,
    choice: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not assistant_enabled() or user is None or not getattr(user, "is_authenticated", False):
        return None
    try:
        if assistant_action is None and (choice or action):
            changed = _change_choice(user, conversation, choice)
            if changed is not None:
                return changed
            return _start_choice(user, conversation, choice, action)
        if assistant_action is not None:
            out = _dispatch_action(user, conversation, assistant_action)
            meta = (out or {}).get("metadata") or {}
            if meta.get("executable") and meta.get("proposal_id"):
                ba_state.save(conversation, pending_proposal_id=meta["proposal_id"])
            return out
        from iic_booking.research_copilot.services.intelligence import security

        if security.detect_injection(text or ""):
            return None
        blocked = _typed_confirm_reply(conversation, text or "")
        if blocked is not None:
            return blocked
        from iic_booking.research_copilot.services.assistant import guided

        if _START_BOOK_RE.match(normalize(text or "")):
            return guided.handle(user, conversation, {"step": "start"})
        if guided.active(conversation):
            typed = guided.handle_text(user, conversation, text or "")
            if typed is not None:
                return typed
        intel_busy = False
        st: dict[str, Any] = {}
        try:
            from iic_booking.research_copilot.services.intelligence import state as intel_state

            st = intel_state.load(conversation)
            intel_busy = bool(st.get("pending_choice") or st.get("step"))
        except Exception:  # noqa: BLE001
            intel_busy = False
        if st.get("step") == "confirm" and resolve_confirm(text or ""):
            return None
        daily_out = _daily(user, conversation, text or "", st)
        if daily_out is not None:
            return daily_out
        return _classify(user, conversation, text or "", ba_state.load(conversation), intel_busy)
    except Exception:  # noqa: BLE001
        logger.exception("booking assistant turn failed")
        if assistant_action is not None:
            return C.reply(
                "Something went wrong while handling that. Please try again, or continue on the booking page.",
                actions=[C.link("Book equipment", "/book-equipment")],
                intent="error",
            )
        return None
