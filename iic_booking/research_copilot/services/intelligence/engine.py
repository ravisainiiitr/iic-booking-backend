"""
Research Copilot intelligence engine.

Runs before the V2 deterministic orchestrator when RESEARCH_COPILOT_INTELLIGENCE_ENABLED is on.
Per message it either answers, asks a targeted clarification, offers choices, prepares a portal
action for explicit confirmation, or offers a support ticket. Returning None hands the turn to the
existing V2 reads (bookings, wallet, results, confirmations) and then RAG/LLM, unchanged.

Nothing here executes a booking, cancellation, reschedule, payment or ticket: those run only via
the explicit confirmation / escalation endpoints.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from iic_booking.research_copilot.services.intelligence import booking_changes as changes
from iic_booking.research_copilot.services.intelligence import entities as entity_svc
from iic_booking.research_copilot.services.intelligence import equipment as eqsvc
from iic_booking.research_copilot.services.intelligence import flows
from iic_booking.research_copilot.services.intelligence import intents as I
from iic_booking.research_copilot.services.intelligence import (
    conversational_actions_enabled,
    intelligence_enabled,
    knowledge_enabled,
)
from iic_booking.research_copilot.services.intelligence import messages as M
from iic_booking.research_copilot.services.intelligence import security
from iic_booking.research_copilot.services.intelligence import state as st
from iic_booking.research_copilot.services.intelligence import terminology
from iic_booking.research_copilot.services.intelligence.entities import Entities
from iic_booking.research_copilot.services.intelligence.flows import Turn

logger = logging.getLogger(__name__)

OPENING_OPTIONS = (
    ("book", "Book equipment"),
    ("availability", "Check availability"),
    ("estimate", "Estimate cost"),
    ("cancel", "Cancel a booking"),
    ("reschedule", "Reschedule a booking"),
    ("help", "Portal help"),
)
TERM_ACTIONS = (
    ("book", "Book"),
    ("view", "View instruments"),
    ("availability", "Check availability"),
    ("estimate", "Estimate cost"),
    ("learn", "Learn about the technique"),
    ("related", "Find related techniques"),
    ("other", "Something else"),
)
_MANUAL_RE = re.compile(
    r"\b(how\s+(do|to|should|can)|prepar\w*|procedure|manual|sop|protocol|mount\w*|coat\w*|sputter\w*|requirement\w*|"
    r"sample\s+(size|amount|quantity|form)|troubleshoot\w*|operat\w*|calibrat\w*)\b"
)
_HELP_INTENTS = {I.PORTAL_HELP, I.MY_RESEARCH, I.RESEARCH_GROUP, I.UNKNOWN}
_MULTI_VALUE_KINDS = {"cancel_slots"}


def _is_v2_confirmation(text: str) -> bool:
    from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent

    try:
        return resolve_intent(text).intent == "confirm_proposal"
    except Exception:  # noqa: BLE001
        return False


_TITLE_SUFFIX = {
    I.BOOKING_REQUEST: "booking",
    I.AVAILABILITY: "availability",
    I.COST_ESTIMATE: "cost estimate",
    I.EQUIPMENT_SEARCH: "instruments",
    I.EQUIPMENT_INFORMATION: "overview",
    I.EQUIPMENT_RECOMMENDATION: "related techniques",
}
_TITLE_FIXED = {
    I.CANCELLATION_REQUEST: "Cancel booking",
    I.PARTIAL_CANCELLATION: "Cancel booking",
    I.RESCHEDULING_REQUEST: "Reschedule booking",
    I.MY_BOOKINGS: "My bookings",
    I.WALLET_BALANCE: "Wallet balance",
    I.WALLET_TRANSACTIONS: "Wallet transactions",
    I.WALLET_RECHARGE: "Wallet recharge",
    I.CREDIT_STATUS: "Wallet credit",
    I.RESULT_STATUS: "Results",
    I.SAMPLE_STATUS: "Sample status",
    I.MY_RESEARCH: "My Research",
    I.RESEARCH_GROUP: "Research groups",
    I.SUPPORT_REQUEST: "Support request",
    I.EQUIPMENT_COMPARISON: "Compare techniques",
}
_TITLE_BY_TOPIC = {
    "faculty_association": "Faculty association",
    "credit_settlement": "Credit settlement",
}


def conversation_title(intent: str, technique: str | None, text: str, *, topic: str | None = None) -> str:
    """Short, readable history titles such as "FESEM booking", "XRD availability", "Wallet recharge"."""
    if topic in _TITLE_BY_TOPIC:
        return _TITLE_BY_TOPIC[topic]
    short = terminology.TECHNIQUES[technique].label.split(" (")[0] if technique in terminology.TECHNIQUES else ""
    if short and intent in _TITLE_SUFFIX:
        return f"{short} {_TITLE_SUFFIX[intent]}"[:80]
    if short and intent == I.BARE_TERM:
        return short
    if intent in _TITLE_FIXED:
        return _TITLE_FIXED[intent]
    if topic:
        from iic_booking.research_copilot.services.intelligence import topics

        if topic in topics.TOPICS:
            return topics.TOPICS[topic].title
    if intent == I.PORTAL_HELP:
        return "Portal help"
    cleaned = " ".join((text or "").split())
    return (cleaned[:77] + "...") if len(cleaned) > 80 else cleaned


def _title_for(intent: str, ents: Entities, text: str) -> str:
    tech = ", ".join(terminology.TECHNIQUES[k].label.split(" (")[0] for k in (ents.techniques or [])[:2])
    base = {
        I.BOOKING_REQUEST: "Book",
        I.AVAILABILITY: "Availability",
        I.COST_ESTIMATE: "Cost estimate",
        I.CANCELLATION_REQUEST: "Cancel booking",
        I.PARTIAL_CANCELLATION: "Cancel booking",
        I.RESCHEDULING_REQUEST: "Reschedule booking",
        I.EQUIPMENT_SEARCH: "Find equipment",
        I.EQUIPMENT_INFORMATION: "Equipment details",
        I.EQUIPMENT_COMPARISON: "Compare techniques",
        I.EQUIPMENT_RECOMMENDATION: "Suggest a technique",
        I.BARE_TERM: tech or "Equipment",
        I.SUPPORT_REQUEST: "Support request",
    }.get(intent)
    if base and tech and intent != I.BARE_TERM:
        return f"{base}: {tech}"[:80]
    if base:
        return base[:80]
    cleaned = " ".join((text or "").split())
    return (cleaned[:77] + "...") if len(cleaned) > 80 else cleaned


# --------------------------------------------------------------------------- simple responses


def opening(turn: Turn) -> dict[str, Any]:
    if conversational_actions_enabled():
        from iic_booking.research_copilot.services.intelligence import topics

        resp = topics.menu(turn, "help")
        resp["content"] = "Hello! What can I help you with? You can type a question or pick a topic."
        resp["metadata"]["title_defer"] = True
        return resp
    options = [{"value": v, "label": label} for v, label in OPENING_OPTIONS]
    st.restart(turn.state)
    st.set_choice(turn.state, kind="start", prompt="How can I help?", options=options)
    return turn.respond(
        message_type=M.CHOICE_LIST,
        content="Hello! How can I help? You can type a question or pick an option.",
        cards=[M.choice_card("start", "How can I help?", options)],
        actions=[M.choice("start", v, label) for v, label in OPENING_OPTIONS]
        + [M.prompt("my_bookings", "My bookings", "List my recent bookings.")],
        source_label=M.SOURCE_COPILOT,
    )


def bare_term(turn: Turn, tech) -> dict[str, Any]:
    _rows, total = eqsvc.search(user=turn.user, technique_keys=[tech.key], limit=1)
    allowed = TERM_ACTIONS if total else tuple(a for a in TERM_ACTIONS if a[0] in {"learn", "related", "other"})
    options = [{"value": v, "label": label} for v, label in allowed]
    st.restart(turn.state, "term", technique=tech.key)
    st.set_choice(turn.state, kind="term_action", prompt=tech.label, options=options)
    short = tech.label.split(" (")[0]
    intro = (
        f"What would you like to do with **{short}**?"
        if total
        else f"No {short} instrument is listed in the IIC catalogue for your account. What would you like to do?"
    )
    if conversational_actions_enabled():
        from iic_booking.research_copilot.services.intelligence import dispatch

        turn.state["context_technique"] = tech.key
        return turn.respond(
            message_type=M.CHOICE_LIST,
            content=intro,
            actions=dispatch.technique_actions(tech.key, has_equipment=bool(total)),
            source_label=M.SOURCE_EQUIPMENT,
            extra={"technique": tech.key, "response_type": "CLARIFICATION"},
        )
    return turn.respond(
        message_type=M.CHOICE_LIST,
        content=intro,
        cards=[M.choice_card("term_action", intro, options)],
        actions=[M.choice("term_action", o["value"], o["label"]) for o in options],
        source_label=M.SOURCE_EQUIPMENT,
    )


def related_techniques(turn: Turn, key: str) -> dict[str, Any]:
    related: list[str] = []
    for group in terminology.PURPOSE_GROUPS.values():
        if key in group:
            related.extend(k for k in group if k != key)
    related = eqsvc.techniques_with_equipment(turn.user, list(dict.fromkeys(related)))[:6]
    if not related:
        return turn.respond(message_type=M.TEXT, content="I don't have related techniques listed for that one.",
                            source_label=M.SOURCE_TECHNIQUE)
    turn.state["workflow"] = "equipment"
    return flows.recommend(Turn(turn.user, turn.text, turn.conversation, turn.state,
                                Entities(purpose_techniques=related), turn.intent, turn.confidence))


def support_offer(turn: Turn) -> dict[str, Any]:
    st.restart(turn.state, "support")
    return turn.respond(
        message_type=M.SUPPORT_TICKET,
        content="I can raise a support ticket for the IIC team with this conversation attached. "
        "Describe the problem in a message first if you haven't, then press Raise Support Ticket. "
        "Nothing is sent until you do.",
        cards=[{"type": "support_offer", "reason": "user_requested"}],
        actions=[M.ticket_action("user_requested"), M.link("tickets", "My support tickets", "/tickets")],
        source_label=M.SOURCE_SUPPORT,
    )


# --------------------------------------------------------------------------- knowledge


def _legacy_docs_strong(turn: Turn) -> bool:
    from iic_booking.research_copilot.services import rag as rag_svc
    from iic_booking.research_copilot.services.context_builder import build_context

    try:
        ctx = build_context(turn.user)
        res = rag_svc.retrieve(query=turn.text, role_bucket=ctx.role_bucket, department_id=ctx.department_id,
                               user=turn.user, conversation=turn.conversation)
    except Exception:  # noqa: BLE001
        return False
    return bool(res.citations) and not res.low_confidence and res.citations[0].score >= 0.6


def _record_gap(turn: Turn) -> None:
    from iic_booking.research_copilot.models import KnowledgeGap

    try:
        KnowledgeGap.objects.create(
            conversation=turn.conversation,
            user=turn.user,
            query_summary=turn.text[:512],
            reason="no_verified_answer",
            intent=turn.intent[:64],
            suggested_faq=f"Q: {turn.text[:200]}\nA: (needs a verified answer)",
        )
    except Exception:  # noqa: BLE001
        logger.warning("knowledge gap write failed", exc_info=True)


def knowledge_answer(turn: Turn, article, related=None) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import knowledge

    knowledge.record_usage(article)
    st.restart(turn.state)
    options = [{"value": str(h.article.id), "label": h.article.title} for h in (related or [])[:3]]
    if options:
        st.set_choice(turn.state, kind="knowledge", prompt="Related", options=options)
    content = f"**{article.title}**\n\n{article.answer}"
    return turn.respond(
        message_type=M.KNOWLEDGE_ANSWER,
        content=content,
        cards=[{"type": "knowledge_answer", "article_id": str(article.id), "title": article.title,
                "category": article.category, "updated_at": article.updated_at.isoformat() if article.updated_at else None}],
        actions=[M.choice("knowledge", o["value"], o["label"]) for o in options],
        source_label=M.SOURCE_KNOWLEDGE,
        extra={"knowledge_article_id": str(article.id)},
    )


def help_answer(turn: Turn) -> dict[str, Any] | None:
    from iic_booking.research_copilot.services.intelligence import knowledge

    if not knowledge_enabled():
        return None
    band, hits = knowledge.best(text=turn.text, user=turn.user)
    turn.confidence = turn.confidence or band
    if band in {knowledge.HIGH, knowledge.MEDIUM}:
        return knowledge_answer(turn, hits[0].article, related=[h for h in hits[1:] if h.confidence != knowledge.LOW])
    if band == knowledge.LOW:
        options = [{"value": str(h.article.id), "label": h.article.title} for h in hits[:3]]
        options.append({"value": "none", "label": "None of these"})
        st.restart(turn.state, "help", question=turn.text[:500])
        st.set_choice(turn.state, kind="knowledge", prompt="Did you mean", options=options)
        return turn.respond(
            message_type=M.CHOICE_LIST,
            content="I'm not sure I understood. Did you mean one of these?",
            cards=[M.choice_card("knowledge", "Did you mean", options, allow_text=True)],
            actions=[M.choice("knowledge", o["value"], o["label"]) for o in options],
            source_label=M.SOURCE_KNOWLEDGE,
        )
    if _legacy_docs_strong(turn):
        return None
    _record_gap(turn)
    st.restart(turn.state, "help", question=turn.text[:500])
    return M.no_verified_answer(intent=turn.intent)


# --------------------------------------------------------------------------- dispatch


def _with(turn: Turn, **ents) -> Turn:
    return Turn(turn.user, turn.text, turn.conversation, turn.state, Entities(**ents), turn.intent, turn.confidence)


def _equipment_action(turn: Turn, value: str) -> dict[str, Any]:
    s = turn.state
    action, _, raw = value.partition(":")
    if action == "other":
        st.restart(s)
        return turn.respond(message_type=M.TEXT, content="Sure, what would you like to do?", source_label=M.SOURCE_COPILOT)
    if action == "more":
        return flows.show_more(turn, int(raw) if raw.isdigit() else 0)
    eq = eqsvc.get_visible(turn.user, raw)
    if eq is None:
        return M.error("That equipment is not available to your account.", intent=turn.intent)
    if action == "pick":
        action = {"book": "book", "slots": "slots", "estimate": "estimate"}.get(s.get("list_purpose") or "", "view")
    if action == "view":
        return flows.equipment_card(turn, eq)
    if action == "slots":
        st.restart(s, "availability", period=s.get("period"), date_text=s.get("date_text"))
        return flows.show_slots(turn, eq)
    if action == "estimate":
        st.restart(s, "estimate", samples=s.get("samples"))
        return flows.estimate_for(turn, [eq], s.get("samples"))
    if action == "book":
        keep = {k: s.get(k) for k in ("samples", "period", "date_text", "earliest", "input_values")}
        if s.get("workflow") != "booking":
            keep["input_values"] = None
        st.restart(s, "booking", **keep)
        flows.remember_equipment(s, eq)
        return flows.continue_booking(turn)
    return M.error("That option is not available.", intent=turn.intent)


def handle_choice(turn: Turn, kind: str, value: str) -> dict[str, Any] | None:
    s = turn.state
    if kind not in _MULTI_VALUE_KINDS:
        st.clear_choice(s)
    if kind == "start":
        st.restart(s)
        if value == "book":
            return flows.technique_prompt(turn, workflow="booking", question="Which equipment would you like to book?")
        if value == "availability":
            return flows.technique_prompt(turn, workflow="availability", question="Which equipment should I check?")
        if value == "estimate":
            return flows.technique_prompt(turn, workflow="estimate", question="Which equipment should I estimate?")
        if value == "cancel":
            return changes.start_cancel(_with(turn))
        if value == "reschedule":
            return changes.start_reschedule(_with(turn))
        st.restart(s, "help")
        return turn.respond(message_type=M.TEXT, content="Ask me your question about the IIC portal.",
                            source_label=M.SOURCE_COPILOT)
    if kind == "term_action":
        key = s.get("technique")
        if key not in terminology.TECHNIQUES:
            return opening(turn)
        t = _with(turn, techniques=[key])
        if value == "book":
            return flows.start_booking(t)
        if value == "view":
            return flows.search_equipment(t)
        if value == "availability":
            return flows.start_availability(t)
        if value == "estimate":
            return flows.start_estimate(t)
        if value == "learn":
            return flows.technique_overview(turn, key)
        if value == "related":
            return related_techniques(turn, key)
        st.restart(s)
        return turn.respond(message_type=M.TEXT, content=f"Tell me what you'd like to do with {key.upper()}.",
                            source_label=M.SOURCE_COPILOT)
    if kind == "technique":
        if value not in terminology.TECHNIQUES:
            return M.error("That technique is not available.", intent=turn.intent)
        t = _with(turn, techniques=[value])
        workflow = s.get("workflow")
        if workflow == "booking":
            return flows.start_booking(t)
        if workflow == "availability":
            return flows.start_availability(t)
        if workflow == "estimate":
            return flows.start_estimate(t)
        return flows.search_equipment(t)
    if kind == "equipment_action":
        return _equipment_action(turn, value)
    if kind == "samples":
        count = entity_svc.parse_count(value)
        if not count:
            return M.error("Enter the number of samples.", intent=turn.intent)
        s["samples"] = count
        s.pop("slot_ids", None)
        return flows.continue_booking(turn)
    if kind == "field":
        key = s.get("field_key")
        if not key:
            return flows.continue_booking(turn)
        inputs = dict(s.get("input_values") or {})
        inputs[str(key)] = str(value)[:500]
        s["input_values"] = inputs
        s.pop("field_key", None)
        return flows.continue_booking(turn)
    if kind == "slot":
        return flows.choose_slot(turn, value)
    if kind == "booking_edit":
        if value == "slot":
            s.pop("slot_ids", None)
            s.pop("earliest", None)
            return flows.continue_booking(turn)
        if value == "samples":
            s.pop("samples", None)
            s.pop("slot_ids", None)
            return flows.continue_booking(turn)
        st.restart(s)
        return turn.respond(message_type=M.TEXT, content="OK, I've stopped. Nothing was booked.", source_label=M.SOURCE_COPILOT)
    if kind == "cancel_booking":
        b = changes._owned(turn.user, value)
        return changes.cancel_selected(turn, b) if b else M.error("That booking is no longer active.", intent=turn.intent)
    if kind == "cancel_mode":
        return changes.cancel_mode(turn, value)
    if kind == "cancel_slots":
        return changes.cancel_slots_chosen(turn, value)
    if kind == "cancel_reduce":
        count = entity_svc.parse_count(value)
        return changes.cancel_reduce_chosen(turn, int(count or 0))
    if kind == "reschedule_booking":
        b = changes._owned(turn.user, value)
        return changes.reschedule_selected(turn, b) if b else M.error("That booking is no longer active.", intent=turn.intent)
    if kind == "reschedule_slot":
        return changes.reschedule_slot_chosen(turn, value)
    if kind == "knowledge":
        from iic_booking.research_copilot.services.intelligence import knowledge

        if value == "none":
            turn.text = s.get("question") or turn.text
            _record_gap(turn)
            return M.no_verified_answer(intent=turn.intent)
        article = knowledge.get_approved(article_id=value, user=turn.user)
        if article is None:
            return M.no_verified_answer(intent=turn.intent)
        return knowledge_answer(turn, article)
    return None


def _free_input(turn: Turn) -> dict[str, Any] | None:
    """Typed answers to the question Copilot just asked (sample count, a form field, samples to keep)."""
    s = turn.state
    step = s.get("step")
    if step == "ask_samples":
        count = entity_svc.parse_count(turn.text)
        if count:
            st.clear_choice(s)
            return handle_choice(turn, "samples", str(count))
    if step == "ask_field" and s.get("field_key") and not s.get("pending_choice"):
        ftype = s.get("field_type")
        raw = turn.text.strip()
        if ftype == "NUMERIC":
            m = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*[a-zA-Z%]*\s*", raw)
            if m:
                return handle_choice(turn, "field", m.group(1))
        elif ftype == "TEXT" and raw and len(raw) <= 500 and I.classify(raw, turn.ents).confidence != I.HIGH:
            return handle_choice(turn, "field", raw)
    if step == "cancel_reduce":
        count = entity_svc.parse_count(turn.text)
        if count:
            st.clear_choice(s)
            return changes.cancel_reduce_chosen(turn, count)
    if (
        step == "choose_slot"
        and conversational_actions_enabled()
        and s.get("equipment_id")
        and turn.ents.has_date
        and not turn.ents.techniques
        and len(turn.text) <= 60
    ):
        st.clear_choice(s)
        return flows.change_slot_window(turn, turn.text)
    return None


def _route(turn: Turn) -> dict[str, Any] | None:
    intent = turn.intent
    ents = turn.ents
    if intent == I.GREETING:
        return opening(turn)
    if intent == I.BARE_TERM:
        tech = terminology.bare_technique(turn.text)
        return bare_term(turn, tech) if tech else None
    if conversational_actions_enabled():
        from iic_booking.research_copilot.services.intelligence import dispatch

        mapped = dispatch.action_for_text(turn)
        if mapped is not None:
            return dispatch.dispatch(turn, *mapped)
    if intent in I.LIVE_READ_INTENTS:
        return None
    if intent == I.SUPPORT_REQUEST:
        return support_offer(turn)
    if intent in {I.CANCELLATION_REQUEST, I.PARTIAL_CANCELLATION}:
        return changes.start_cancel(turn)
    if intent == I.RESCHEDULING_REQUEST:
        return changes.start_reschedule(turn)
    if intent == I.BOOKING_REQUEST:
        return flows.start_booking(turn)
    if intent == I.AVAILABILITY:
        return flows.start_availability(turn)
    if intent == I.COST_ESTIMATE:
        return flows.start_estimate(turn)
    if intent == I.EQUIPMENT_COMPARISON:
        return flows.compare(turn)
    if intent == I.EQUIPMENT_RECOMMENDATION:
        return flows.recommend(turn)
    if intent == I.EQUIPMENT_SEARCH:
        return flows.search_equipment(turn)
    if intent == I.EQUIPMENT_INFORMATION:
        if _MANUAL_RE.search(terminology.normalize(turn.text)):
            return None
        eq, rows, _total = flows.resolve_for(turn)
        if eq is not None:
            return flows.equipment_card(turn, eq)
        if ents.techniques:
            return flows.technique_overview(turn, ents.techniques[0])
        return help_answer(turn)
    if intent in _HELP_INTENTS:
        return help_answer(turn)
    return None


_EQUIPMENT_INTENTS = {I.BOOKING_REQUEST, I.AVAILABILITY, I.COST_ESTIMATE, I.EQUIPMENT_SEARCH, I.EQUIPMENT_INFORMATION}
_CONTEXT_WORDS = {"it", "that", "one", "same", "them", "again", "also", "then", "ok", "okay", "yes", "technique"}


def _apply_context(turn: Turn) -> None:
    """ "book it" / "how much does it cost" right after "fesem" means FESEM (a named instrument still wins)."""
    key = turn.state.get("context_technique")
    if key not in terminology.TECHNIQUES or turn.ents.techniques or turn.intent not in _EQUIPMENT_INTENTS:
        return
    if set(eqsvc.equipment_query(turn.text).split()) <= _CONTEXT_WORDS:
        turn.ents.techniques = [key]


def _conversational_title(meta, state, turn: Turn, text: str, action, topic_key: str | None) -> None:
    """A topic menu gives a provisional title ("Wallet") that the first concrete step replaces ("Wallet recharge")."""
    if state.get("title_auto") or meta.get("title_defer"):
        return
    payload = (action or {}).get("payload") or {}
    technique = (turn.ents.techniques or [None])[0] or payload.get("technique") or meta.get("technique")
    if technique is None and turn.intent in _EQUIPMENT_INTENTS:
        technique = state.get("context_technique")
    title = conversation_title(turn.intent, technique, text, topic=topic_key or meta.get("topic"))
    if state.get("provisional_title"):
        meta["replace_title"] = state["provisional_title"]
    meta["title_hint"] = title
    if meta.get("response_type") == "CLARIFICATION" or topic_key == "equipment":
        state["provisional_title"] = title
    else:
        state.pop("provisional_title", None)
        state["title_auto"] = True


def try_intelligent_turn(
    *,
    user,
    text: str,
    conversation=None,
    choice: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not intelligence_enabled() or user is None or not getattr(user, "is_authenticated", False):
        return None
    text = (text or "").strip()
    state = st.load(conversation)

    flagged = security.detect_injection(text)
    if flagged:
        return M.envelope(message_type=M.TEXT, content=security.REFUSAL, source_label=M.SOURCE_COPILOT,
                          intent="SECURITY_REFUSAL", extra={"security_flag": flagged})

    ents = entity_svc.extract(text)
    turn = Turn(user=user, text=text, conversation=conversation, state=state, ents=ents)
    response: dict[str, Any] | None = None
    conversational = conversational_actions_enabled()
    topic_key: str | None = None

    try:
        if action and conversational:
            from iic_booking.research_copilot.services.intelligence import dispatch

            topic_key = (action.get("payload") or {}).get("topic")
            response = dispatch.dispatch(turn, action["type"], action.get("payload") or {})
            state["last_intent"] = turn.intent
        elif choice and choice.get("kind"):
            kind = str(choice.get("kind"))
            value = str(choice.get("value") or "")
            if kind == "start":
                # Fixed menu (no record ids), so quick-action buttons may start a flow at any time.
                valid = value in {v for v, _label in OPENING_OPTIONS}
            elif kind in _MULTI_VALUE_KINDS:
                valid = (state.get("pending_choice") or {}).get("kind") == kind
            else:
                valid = st.match_choice(state, value=value, kind=kind) is not None
            if not valid:
                response = M.error("That option has expired. Please ask again or pick from the latest options.",
                                   intent="CHOICE_EXPIRED")
            else:
                turn.intent = state.get("last_intent") or ""
                response = handle_choice(turn, kind, value)
        else:
            if state.get("step") == "confirm" and _is_v2_confirmation(text):
                return None
            response = _free_input(turn)
            if response is None and state.get("pending_choice"):
                picked = st.match_choice(state, text=text)
                if picked is not None:
                    turn.intent = state.get("last_intent") or ""
                    response = handle_choice(turn, state["pending_choice"]["kind"], str(picked["value"]))
            if response is None and conversational:
                from iic_booking.research_copilot.services.intelligence import topics

                topic_key = topics.bare_topic(text)
                if topic_key:
                    turn.intent = I.PORTAL_HELP
                    st.clear_choice(state)
                    response = topics.menu(turn, topic_key)
                    if topic_key == "help":
                        response["metadata"]["title_defer"] = True
            if response is None:
                result = I.classify(text, ents)
                turn.intent, turn.confidence = result.intent, result.confidence
                if conversational:
                    _apply_context(turn)
                if state.get("pending_choice") and (result.intent == I.UNKNOWN or result.confidence == I.LOW):
                    pending = state["pending_choice"]
                    response = M.envelope(
                        message_type=M.CHOICE_LIST,
                        content="Please pick one of the options, or tell me what you'd like to do instead.",
                        cards=[M.choice_card(pending["kind"], pending.get("prompt") or "", pending.get("options") or [])],
                        actions=[M.choice(pending["kind"], o["value"], o["label"]) for o in (pending.get("options") or [])[:8]],
                        intent=turn.intent,
                    )
                else:
                    response = _route(turn)
                    if response is not None:
                        state["last_intent"] = turn.intent
    except Exception:  # noqa: BLE001
        logger.exception("copilot intelligence turn failed")
        return None

    if response is None:
        return None
    meta = dict(response.get("metadata") or {})
    meta.setdefault("intent", turn.intent)
    meta["entities"] = ents.as_dict()
    meta["question"] = (turn.text or text)[:500]
    if conversational:
        _conversational_title(meta, state, turn, text, action, topic_key)
    elif not state.get("title_auto"):
        meta["title_hint"] = _title_for(turn.intent, ents, text)
        state["title_auto"] = True
    response["metadata"] = meta
    state["last_question"] = meta["question"]
    st.save(conversation, state)
    return response
