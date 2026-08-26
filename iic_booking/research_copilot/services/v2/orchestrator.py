"""Phase A deterministic-first orchestrator."""

from __future__ import annotations

from typing import Any

from django.conf import settings

from iic_booking.research_copilot.services.v2 import flag, v2_enabled
from iic_booking.research_copilot.services.v2.intent_resolver import resolve_intent
from iic_booking.research_copilot.services.v2 import read_tools


def _context_equipment_id(conversation) -> int | None:
    if conversation is None:
        return None
    from django.core.cache import cache

    meta = cache.get(f"copilot_ctx:{conversation.id}") or {}
    if isinstance(meta, dict):
        eid = meta.get("last_equipment_id")
        try:
            return int(eid) if eid is not None else None
        except (TypeError, ValueError):
            return None
    return None


def _store_context(conversation, metadata: dict[str, Any]) -> None:
    if conversation is None:
        return
    eid = metadata.get("equipment_id")
    if not eid:
        return
    from django.core.cache import cache

    meta = cache.get(f"copilot_ctx:{conversation.id}") or {}
    if not isinstance(meta, dict):
        meta = {}
    meta["last_equipment_id"] = eid
    if metadata.get("equipment_name"):
        meta["last_equipment_name"] = metadata.get("equipment_name")
    cache.set(f"copilot_ctx:{conversation.id}", meta, 3600 * 6)


def try_deterministic_turn(*, user, text: str, conversation=None, public: bool = False) -> dict[str, Any] | None:
    """
    If this turn can be answered without the LLM, return a response envelope.
    Otherwise return None so the caller continues with RAG/LLM.
    """
    if not v2_enabled() or not flag("COPILOT_DETERMINISTIC_READS", True):
        return None

    intent = resolve_intent(text)
    if not intent.deterministic:
        return None

    if intent.needs_auth and (user is None or not getattr(user, "is_authenticated", False)):
        from iic_booking.research_copilot.services.v2.response_builder import build_response

        return build_response(
            kind="ACTION_REQUIRED",
            content="Sign in to use personal Copilot tools (bookings, wallet, results).",
            actions=[{"id": "sign_in", "label": "Sign in", "href": "/auth", "enabled": True}],
            metadata={"intent": intent.intent, "deterministic": True},
        )

    ctx_eq = _context_equipment_id(conversation)
    result: dict[str, Any] | None = None

    if intent.intent == "search_slots":
        result = read_tools.search_available_slots(user=user, text=text, context_equipment_id=ctx_eq)
    elif intent.intent == "search_equipment":
        result = read_tools.search_equipment_catalog(user=user, text=text)
    elif intent.intent == "estimate_cost":
        result = read_tools.estimate_cost(user=user, text=text, context_equipment_id=ctx_eq)
    elif intent.intent == "my_bookings":
        result = read_tools.my_bookings(user=user)
    elif intent.intent == "next_booking":
        result = read_tools.next_booking(user=user)
    elif intent.intent == "wallet_balance":
        result = read_tools.wallet_balance(user=user)
    elif intent.intent == "wallet_transactions":
        result = read_tools.wallet_transactions(user=user)
    elif intent.intent == "sample_status":
        result = read_tools.sample_or_results(user=user, text=text, which="sample")
    elif intent.intent == "results":
        result = read_tools.sample_or_results(user=user, text=text, which="results")
    elif intent.intent == "ra_status":
        result = read_tools.ra_status(user=user)
    elif intent.intent == "affiliations":
        result = read_tools.affiliations(user=user)
    elif intent.intent == "pending_actions":
        result = read_tools.pending_actions(user=user)
    elif intent.intent == "docs_rag":
        result = read_tools.docs_rag(user=user, text=text)
    else:
        return None

    if result is None:
        return None

    meta = dict(result.get("metadata") or {})
    meta["intent"] = intent.intent
    meta["v2"] = True
    meta["llm_used"] = False
    result["metadata"] = meta
    _store_context(conversation, meta)
    return result
