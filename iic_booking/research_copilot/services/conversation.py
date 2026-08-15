"""Conversation orchestration for IIC Research Copilot."""

from __future__ import annotations

import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iic_booking.research_copilot.constants import (
    CONFIDENCE_ESCALATE_THRESHOLD,
    ESCALATE_MARKER,
    SUGGESTED_PROMPTS,
)
from iic_booking.research_copilot.models import (
    Conversation,
    KnowledgeGap,
    Message,
    MessageFeedback,
    MessageRole,
)
from iic_booking.research_copilot.services import audit as audit_svc
from iic_booking.research_copilot.services.context_builder import build_context
from iic_booking.research_copilot.services.llm_gateway import default_max_tokens, get_gateway
from iic_booking.research_copilot.services.prompt_builder import (
    append_retrieval_context,
    build_messages_for_llm,
    build_system_prompt,
)
from iic_booking.research_copilot.services import rag as rag_svc
from iic_booking.research_copilot.services import tools as tools_svc


def feature_enabled(*, user=None) -> bool:
    """
    Global enable via RESEARCH_COPILOT_ENABLED.

    AI.24.1:
      - Anonymous: ENABLED and RESEARCH_COPILOT_PUBLIC_ENABLED
      - Authenticated: ENABLED and (PUBLIC_ENABLED or on pilot allowlist / empty allowlist)

    Private tools still require authenticated_full_access().
    """
    if not bool(getattr(settings, "RESEARCH_COPILOT_ENABLED", False)):
        return False
    if user is None or not getattr(user, "is_authenticated", False):
        return bool(getattr(settings, "RESEARCH_COPILOT_PUBLIC_ENABLED", True))
    if bool(getattr(settings, "RESEARCH_COPILOT_PUBLIC_ENABLED", True)):
        return True
    return authenticated_full_access(user=user)


def public_mode_enabled() -> bool:
    return bool(getattr(settings, "RESEARCH_COPILOT_ENABLED", False)) and bool(
        getattr(settings, "RESEARCH_COPILOT_PUBLIC_ENABLED", True)
    )


def authenticated_full_access(*, user) -> bool:
    """True when the user may use private/authorized tools (pilot rules apply)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if not bool(getattr(settings, "RESEARCH_COPILOT_ENABLED", False)):
        return False
    raw = (getattr(settings, "RESEARCH_COPILOT_PILOT_EMAILS", None) or "").strip()
    if not raw:
        return True
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not allowed:
        return True
    email = (getattr(user, "email", None) or "").strip().lower()
    return email in allowed


def effective_access_mode(*, user) -> str:
    from iic_booking.research_copilot.services.access_control import AccessMode

    if authenticated_full_access(user=user):
        return AccessMode.AUTHENTICATED.value
    return AccessMode.PUBLIC.value


def _reply_from_llm_result(result) -> str:
    """Map gateway result to user-visible text without exposing stack traces."""
    text = (result.text if result else "") or ""
    if text.strip():
        return text
    category = getattr(result, "error_category", "") if result else ""
    if category:
        return (
            "Research Copilot is temporarily unavailable. "
            "Your booking and other portal operations are unaffected. "
            "Please try again shortly, or open **Tickets** for human support.\n"
            + ESCALATE_MARKER
        )
    return (
        "I could not generate a reply right now. Please try again or open **Tickets** for human support.\n"
        + ESCALATE_MARKER
    )


def _append_sources_footer(reply: str, citations: list) -> str:
    if not citations:
        return reply
    # Avoid duplicating if model already listed Sources
    if "Sources" in reply and any(getattr(c, "title", "") in reply for c in citations[:2]):
        return reply
    lines = ["", "---", "**Sources**"]
    for c in citations:
        title = c.title
        url = c.url or ""
        if url:
            lines.append(f"- [{title}]({url})")
        else:
            lines.append(f"- {title}")
    return reply.rstrip() + "\n" + "\n".join(lines)


def _estimate_confidence(*, escalate: bool, provider: str, text: str, retrieval_low: bool, hit_count: int) -> float:
    if escalate:
        return 0.3
    if retrieval_low or hit_count == 0:
        return 0.4
    if provider == "local":
        return 0.6
    if len(text) < 40:
        return 0.5
    return min(0.92, 0.7 + 0.04 * min(hit_count, 5))


def create_conversation(*, user=None, title: str = "", anonymous_session_key: str = "") -> Conversation:
    ctx = build_context(user)
    mode = effective_access_mode(user=user)
    conv = Conversation.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        anonymous_session_key=(anonymous_session_key or "")[:64] if not getattr(user, "is_authenticated", False) else "",
        access_mode=mode,
        title=(title or "New conversation")[:255],
        user_role_snapshot=(ctx.user_type or mode)[:64],
        department_id_snapshot=ctx.department_id,
    )
    audit_svc.audit_conversation_created(user=user, conversation=conv)
    return conv


def list_conversations(*, user=None, anonymous_session_key: str = "", limit: int = 50):
    if getattr(user, "is_authenticated", False):
        return Conversation.objects.filter(user=user, is_archived=False)[:limit]
    key = (anonymous_session_key or "").strip()
    if not key:
        return Conversation.objects.none()
    return Conversation.objects.filter(
        user__isnull=True,
        anonymous_session_key=key,
        is_archived=False,
        access_mode="public",
    )[:limit]


def get_conversation(*, user=None, conversation_id, anonymous_session_key: str = "") -> Conversation:
    if getattr(user, "is_authenticated", False):
        return Conversation.objects.get(id=conversation_id, user=user)
    key = (anonymous_session_key or "").strip()
    return Conversation.objects.get(
        id=conversation_id,
        user__isnull=True,
        anonymous_session_key=key,
        access_mode="public",
    )


def _suggested_for(ctx) -> list[str]:
    return list(SUGGESTED_PROMPTS.get(ctx.role_bucket) or SUGGESTED_PROMPTS["default"])


def _strip_escalate(text: str) -> tuple[str, bool]:
    escalate = ESCALATE_MARKER in text
    cleaned = text.replace(ESCALATE_MARKER, "").strip()
    # Remove empty trailing lines left by marker
    while cleaned.endswith("\n\n"):
        cleaned = cleaned[:-1]
    return cleaned, escalate


def _static_actions(*, escalate: bool) -> list[dict]:
    actions = [
        {"id": "open_equipments", "label": "Find Equipment", "href": "/equipments", "enabled": True},
        {"id": "open_my_bookings", "label": "My Bookings", "href": "/my-bookings", "enabled": True},
        {"id": "open_wallet", "label": "Open Wallet", "href": "/wallet", "enabled": True},
        {"id": "open_tickets", "label": "Support Tickets", "href": "/tickets", "enabled": True},
    ]
    if escalate:
        actions.insert(
            0,
            {
                "id": "escalate_ticket",
                "label": "Create support ticket",
                "href": "/tickets",
                "enabled": True,
                "hint": "Open Tickets to escalate with conversation context.",
            },
        )
    actions.append(
        {
            "id": "book_equipment",
            "label": "Book Equipment",
            "href": "/book-equipment",
            "enabled": True,
            "requires_confirmation": True,
            "hint": "Opens the portal booking flow — confirm there before anything is created.",
        }
    )
    return actions


def send_message(*, user, conversation: Conversation, content: str) -> dict:
    """
    Persist user message, run portal grounding + RAG, then call LLM.

    Critical path isolation (AI.17):
    - No long-lived DB transaction around Ollama/OpenAI.
    - Concurrency gate rejects overload with a friendly busy message.
    - Failures stay inside Copilot; booking/DSA/RAA are untouched.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("empty_message")
    max_chars = int(getattr(settings, "RESEARCH_COPILOT_MAX_INPUT_CHARS", 4000) or 4000)
    if len(text) > max_chars:
        raise ValueError("message_too_long")
    max_user_msgs = int(getattr(settings, "RESEARCH_COPILOT_MAX_USER_MESSAGES", 40) or 40)
    user_msg_count = conversation.messages.filter(role=MessageRole.USER).count()
    if user_msg_count >= max_user_msgs:
        raise ValueError("conversation_limit_reached")

    ctx = build_context(user)
    with transaction.atomic():
        Message.objects.create(
            conversation=conversation,
            role=MessageRole.USER,
            content=text,
        )

    history = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages.order_by("created_at")
        if m.role in {MessageRole.USER, MessageRole.ASSISTANT}
    ]
    prior = history[:-1]

    from iic_booking.research_copilot.services.portal_grounding import run_portal_grounding
    from iic_booking.research_copilot.services.prompt_builder import append_portal_context
    from iic_booking.research_copilot.services.inference_concurrency import (
        BUSY_USER_MESSAGE,
        CopilotBusyError,
        acquire_generation_slot,
    )
    from iic_booking.research_copilot.models import AuditAction
    from iic_booking.research_copilot.services.intent import detect_intent
    from iic_booking.research_copilot.services.query_intelligence import (
        clarification_question,
        enrich_query_with_history,
        security_refusal,
    )

    prior_user_texts = [h["content"] for h in prior if h.get("role") == MessageRole.USER]
    follow = enrich_query_with_history(text=text, prior_user_texts=prior_user_texts)
    grounded_text = follow.get("text") or text

    refused = security_refusal(text=grounded_text) or security_refusal(text=text)
    if refused:
        with transaction.atomic():
            assistant = Message.objects.create(
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                content=refused,
                confidence=1.0,
                citations=[],
                suggested_actions=_static_actions(escalate=False),
                escalate_hint=False,
                metadata={
                    "provider": "deterministic",
                    "model": "",
                    "intent": "security_refusal",
                    "security_refusal": True,
                    "followup_enriched": bool(follow.get("enriched")),
                    "portal_tools": [],
                    "llm_latency_ms": 0,
                    "rag_skipped": True,
                    "prompt_chars": len(refused),
                },
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])
        audit_svc.audit_message_replied(
            user=user,
            conversation=conversation,
            confidence=1.0,
            escalate=False,
        )
        return {
            "conversation_id": str(conversation.id),
            "message": serialize_message(assistant),
            "suggested_prompts": _suggested_for(ctx),
            "tools_available": tools_svc.list_tools_for_role(
                ctx.role_bucket, access_mode=effective_access_mode(user=user)
            ),
            "access_mode": effective_access_mode(user=user),
            "login_required": False,
        }

    access_mode = effective_access_mode(user=user)
    from iic_booking.research_copilot.services.access_control import (
        LOGIN_REQUIRED_MESSAGE,
        private_intent_requires_login,
        strip_internal_infra,
    )

    # Backend auth boundary — LLM must never decide this (AI.24.1).
    from iic_booking.research_copilot.services.access_control import AccessMode as _AccessMode

    if private_intent_requires_login(
        text=grounded_text, access_mode=_AccessMode(access_mode)
    ):
        login_msg = LOGIN_REQUIRED_MESSAGE
        with transaction.atomic():
            assistant = Message.objects.create(
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                content=login_msg,
                confidence=1.0,
                citations=[],
                suggested_actions=[
                    {
                        "id": "sign_in",
                        "label": "Sign in to continue",
                        "href": "/login",
                        "enabled": True,
                    }
                ],
                escalate_hint=False,
                metadata={
                    "provider": "deterministic",
                    "model": "",
                    "intent": "login_required",
                    "login_required": True,
                    "access_mode": access_mode,
                    "followup_enriched": bool(follow.get("enriched")),
                    "portal_tools": [],
                    "llm_latency_ms": 0,
                    "rag_skipped": True,
                    "prompt_chars": len(login_msg),
                },
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])
        audit_svc.write_audit(
            action=AuditAction.MESSAGE_REPLIED,
            message="login_required",
            user=user,
            conversation=conversation,
            detail={"intent": "login_required", "access_mode": access_mode},
        )
        return {
            "conversation_id": str(conversation.id),
            "message": serialize_message(assistant),
            "suggested_prompts": _suggested_for(ctx),
            "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket, access_mode=access_mode),
            "access_mode": access_mode,
            "login_required": True,
            "login_href": "/login",
        }

    clarify = clarification_question(text=grounded_text)
    if clarify:
        with transaction.atomic():
            assistant = Message.objects.create(
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                content=clarify,
                confidence=0.9,
                citations=[],
                suggested_actions=_static_actions(escalate=False),
                escalate_hint=False,
                metadata={
                    "provider": "deterministic",
                    "model": "",
                    "intent": "clarification",
                    "clarification": True,
                    "followup_enriched": bool(follow.get("enriched")),
                    "portal_tools": [],
                    "llm_latency_ms": 0,
                    "rag_skipped": True,
                    "prompt_chars": len(clarify),
                },
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])
        audit_svc.audit_message_replied(
            user=user,
            conversation=conversation,
            confidence=0.9,
            escalate=False,
        )
        return {
            "conversation_id": str(conversation.id),
            "message": serialize_message(assistant),
            "suggested_prompts": _suggested_for(ctx),
            "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket, access_mode=access_mode),
            "access_mode": access_mode,
            "login_required": False,
        }

    t0 = time.monotonic()
    grounding = run_portal_grounding(user=user, text=grounded_text, access_mode=access_mode)
    t_ground_ms = int((time.monotonic() - t0) * 1000)

    # AI.22.1: equipment-family clarification from grounding (e.g. bare XRD → PXRD vs GI-XRD)
    ground_clarify = (grounding.get("clarification") or "").strip()
    if ground_clarify:
        with transaction.atomic():
            assistant = Message.objects.create(
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                content=ground_clarify,
                confidence=0.92,
                citations=[],
                suggested_actions=_static_actions(escalate=False),
                escalate_hint=False,
                metadata={
                    "provider": "deterministic",
                    "model": "",
                    "intent": "clarification",
                    "clarification": True,
                    "followup_enriched": bool(follow.get("enriched")),
                    "portal_tools": grounding.get("tool_results") or [],
                    "portal_grounding_ms": t_ground_ms,
                    "llm_latency_ms": 0,
                    "rag_skipped": True,
                    "prompt_chars": len(ground_clarify),
                },
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])
        audit_svc.audit_message_replied(
            user=user,
            conversation=conversation,
            confidence=0.92,
            escalate=False,
        )
        return {
            "conversation_id": str(conversation.id),
            "message": serialize_message(assistant),
            "suggested_prompts": _suggested_for(ctx),
            "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket, access_mode=access_mode),
            "access_mode": access_mode,
            "login_required": False,
        }

    # AI.22.2: mixed cost+prepare — answer from portal tools without LLM (Q-U-001 timeout fix).
    deterministic = (grounding.get("deterministic_reply") or "").strip()
    if deterministic:
        deterministic = strip_internal_infra(deterministic)
        base_actions = _static_actions(escalate=False)
        for a in reversed(grounding.get("actions") or []):
            if a.get("id") and all(x.get("id") != a.get("id") for x in base_actions):
                base_actions.insert(0, a)
        with transaction.atomic():
            assistant = Message.objects.create(
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                content=deterministic,
                confidence=0.95,
                citations=[],
                suggested_actions=base_actions,
                escalate_hint=False,
                metadata={
                    "provider": "deterministic",
                    "model": "",
                    "intent": "portal_mixed_cost_prepare",
                    "followup_enriched": bool(follow.get("enriched")),
                    "portal_tools": grounding.get("tool_results") or [],
                    "portal_grounding_ms": t_ground_ms,
                    "llm_latency_ms": 0,
                    "rag_skipped": True,
                    "prompt_chars": len(deterministic),
                    "modes": grounding.get("modes") or [],
                },
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])
        audit_svc.audit_message_replied(
            user=user,
            conversation=conversation,
            confidence=0.95,
            escalate=False,
        )
        return {
            "conversation_id": str(conversation.id),
            "message": serialize_message(assistant),
            "suggested_prompts": _suggested_for(ctx),
            "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket, access_mode=access_mode),
            "access_mode": access_mode,
            "login_required": False,
        }

    # AI.21.2: skip heavy RAG when authoritative portal tools already ground the turn,
    # or the user asked a pure portal lookup (booking/wallet/sample/results).
    intent = detect_intent(grounded_text)
    portal_tools = [t.get("tool") for t in (grounding.get("tool_results") or []) if t.get("tool")]
    portal_only_tools = {
        "get_next_booking",
        "search_bookings",
        "get_wallet",
        "get_sample_status",
        "get_booking_results",
        "get_sample_deadline",
        "search_slots",
        "estimate_booking_cost",
        "recommend_software",
        "search_documentation",
    }
    skip_rag = bool(portal_tools) and set(portal_tools).issubset(portal_only_tools | {"search_equipment"})
    if intent in {"status"} and portal_tools:
        skip_rag = True
    # Prefer portal documentation tool over duplicate RAG when both could fire.
    if "search_documentation" in portal_tools:
        skip_rag = True
    lower = grounded_text.lower()
    # Prepare/SOP without a docs tool still needs knowledge retrieval.
    if (
        any(k in lower for k in ("prepare", "sop", "manual", "guide", "documentation", "how should"))
        and "search_documentation" not in portal_tools
        and not portal_tools
    ):
        skip_rag = False
    # Definitional science questions: allow compact RAG (no portal tools).
    if not portal_tools and any(
        k in lower for k in ("what is", "what's", "whats", "define", "explain", "difference between")
    ):
        skip_rag = False

    t1 = time.monotonic()
    if skip_rag:
        retrieval = rag_svc.RetrievalResult(
            citations=[],
            context_block="",
            intent=intent,
            low_confidence=False,
            latency_ms=0,
        )
    else:
        retrieval = rag_svc.retrieve(
            query=grounded_text,
            role_bucket=ctx.role_bucket,
            department_id=ctx.department_id,
            user=user,
            conversation=conversation,
            limit=3,
        )
    t_rag_ms = int((time.monotonic() - t1) * 1000)
    citations = retrieval.citations
    system = build_system_prompt(ctx)
    system = append_portal_context(system, portal_block=grounding.get("block") or "")
    if not skip_rag:
        system = append_retrieval_context(
            system,
            context_block=retrieval.context_block,
            citations=citations,
        )

    llm_messages = build_messages_for_llm(system_prompt=system, history=prior, user_message=text)
    prompt_chars = sum(len(m.get("content") or "") for m in llm_messages)
    gateway = get_gateway()
    result = None
    busy = False
    try:
        with acquire_generation_slot(wait=False):
            # generate() preferred; complete() remains available on all gateways
            result = gateway.generate(llm_messages, max_tokens=default_max_tokens())
    except CopilotBusyError:
        busy = True
        audit_svc.write_audit(
            action=AuditAction.BUSY,
            message="COPILOT_BUSY",
            user=user,
            conversation=conversation,
            detail={"code": "copilot_busy"},
        )
        result = type("R", (), {"text": BUSY_USER_MESSAGE + "\n" + ESCALATE_MARKER, "provider": "none", "model": "", "error_category": "busy", "latency_ms": 0, "prompt_tokens": None, "completion_tokens": None})()

    raw = _reply_from_llm_result(result)
    reply, escalate = _strip_escalate(raw)
    if not busy:
        reply = _append_sources_footer(reply, citations)
    reply = strip_internal_infra(reply)
    provider = result.provider if result else "none"
    confidence = _estimate_confidence(
        escalate=escalate,
        provider=provider,
        text=reply,
        retrieval_low=retrieval.low_confidence,
        hit_count=len(citations) + len(grounding.get("tool_results") or []),
    )
    if confidence < CONFIDENCE_ESCALATE_THRESHOLD or retrieval.low_confidence:
        escalate = True
    if result and getattr(result, "error_category", "") and not (getattr(result, "text", "") or "").strip():
        escalate = True
    if busy:
        escalate = False
        confidence = 0.5

    base_actions = _static_actions(escalate=escalate)
    for a in reversed(grounding.get("actions") or []):
        if a.get("id") and all(x.get("id") != a.get("id") for x in base_actions):
            base_actions.insert(0, a)

    with transaction.atomic():
        assistant = Message.objects.create(
            conversation=conversation,
            role=MessageRole.ASSISTANT,
            content=reply,
            confidence=confidence,
            citations=rag_svc.citations_as_dicts(citations) if not busy else [],
            suggested_actions=tools_svc.enrich_actions_from_message(
                user=user,
                text=text,
                base_actions=base_actions,
            ),
            escalate_hint=escalate,
            metadata={
                "provider": provider,
                "model": getattr(result, "model", "") if result else "",
                "intent": retrieval.intent,
                "retrieval_latency_ms": retrieval.latency_ms,
                "portal_grounding_ms": t_ground_ms,
                "rag_ms": t_rag_ms,
                "rag_skipped": skip_rag,
                "prompt_chars": prompt_chars,
                "llm_latency_ms": getattr(result, "latency_ms", 0) if result else 0,
                "llm_error_category": getattr(result, "error_category", "") if result else "",
                "prompt_tokens": getattr(result, "prompt_tokens", None) if result else None,
                "completion_tokens": getattr(result, "completion_tokens", None) if result else None,
                "portal_tools": grounding.get("tool_results") or [],
                "response_modes": grounding.get("modes") or [],
                "busy": busy,
                "followup_enriched": bool(follow.get("enriched")),
                "clarification": False,
            },
        )

        if not conversation.title or conversation.title == "New conversation":
            conversation.title = text[:80]
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["title", "updated_at"])

        if not busy and (escalate or retrieval.low_confidence) and getattr(user, "is_authenticated", False):
            KnowledgeGap.objects.create(
                conversation=conversation,
                user=user,
                query_summary=text[:512],
                reason="escalate_hint" if escalate else "low_retrieval",
                suggested_faq=f"Q: {text[:200]}\nA: (needs documentation)",
            )

    if not busy:
        audit_svc.audit_message_replied(
            user=user,
            conversation=conversation,
            confidence=confidence,
            escalate=escalate,
        )

    return {
        "conversation_id": str(conversation.id),
        "message": serialize_message(assistant),
        "suggested_prompts": _suggested_for(ctx),
        "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket, access_mode=access_mode),
        "access_mode": access_mode,
        "login_required": False,
    }


def stream_message_deltas(*, user, conversation: Conversation, content: str):
    """
    Yield SSE-ready dict events. Persists user + assistant messages when stream completes.
    """
    text = (content or "").strip()
    if not text:
        raise ValueError("empty_message")
    max_chars = int(getattr(settings, "RESEARCH_COPILOT_MAX_INPUT_CHARS", 4000) or 4000)
    if len(text) > max_chars:
        raise ValueError("message_too_long")
    max_user_msgs = int(getattr(settings, "RESEARCH_COPILOT_MAX_USER_MESSAGES", 40) or 40)
    if conversation.messages.filter(role=MessageRole.USER).count() >= max_user_msgs:
        raise ValueError("conversation_limit_reached")

    ctx = build_context(user)
    access_mode = effective_access_mode(user=user)
    from iic_booking.research_copilot.services.access_control import (
        AccessMode as _AccessMode,
        LOGIN_REQUIRED_MESSAGE,
        private_intent_requires_login,
        strip_internal_infra,
    )

    Message.objects.create(conversation=conversation, role=MessageRole.USER, content=text)
    prior = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages.order_by("created_at")
        if m.role in {MessageRole.USER, MessageRole.ASSISTANT}
    ][:-1]

    from iic_booking.research_copilot.services.portal_grounding import run_portal_grounding
    from iic_booking.research_copilot.services.prompt_builder import append_portal_context

    if private_intent_requires_login(text=text, access_mode=_AccessMode(access_mode)):
        yield {"event": "token", "text": LOGIN_REQUIRED_MESSAGE}
        yield {
            "event": "done",
            "message": {
                "role": "assistant",
                "content": LOGIN_REQUIRED_MESSAGE,
                "metadata": {"provider": "deterministic", "login_required": True, "access_mode": access_mode},
            },
            "login_required": True,
            "login_href": "/login",
            "access_mode": access_mode,
        }
        return

    grounding = run_portal_grounding(user=user, text=text, access_mode=access_mode)

    # AI.22.2: same short-circuits as send_message (stream path)
    ground_clarify = (grounding.get("clarification") or "").strip()
    if ground_clarify:
        yield {"event": "token", "text": ground_clarify}
        yield {
            "event": "done",
            "message": {
                "role": "assistant",
                "content": ground_clarify,
                "metadata": {
                    "provider": "deterministic",
                    "clarification": True,
                    "portal_tools": grounding.get("tool_results") or [],
                },
            },
        }
        return

    deterministic = (grounding.get("deterministic_reply") or "").strip()
    if deterministic:
        yield {"event": "token", "text": deterministic}
        yield {
            "event": "done",
            "message": {
                "role": "assistant",
                "content": deterministic,
                "metadata": {
                    "provider": "deterministic",
                    "intent": "portal_mixed_cost_prepare",
                    "portal_tools": grounding.get("tool_results") or [],
                    "modes": grounding.get("modes") or [],
                },
            },
        }
        return

    from iic_booking.research_copilot.services.intent import detect_intent

    intent = detect_intent(text)
    portal_tools = [t.get("tool") for t in (grounding.get("tool_results") or []) if t.get("tool")]
    portal_only_tools = {
        "get_next_booking",
        "search_bookings",
        "get_wallet",
        "get_sample_status",
        "get_booking_results",
        "get_sample_deadline",
        "search_slots",
        "estimate_booking_cost",
        "recommend_software",
        "search_documentation",
    }
    skip_rag = bool(portal_tools) and set(portal_tools).issubset(portal_only_tools | {"search_equipment"})
    if intent in {"status"} and portal_tools:
        skip_rag = True
    if "search_documentation" in portal_tools:
        skip_rag = True
    lower = text.lower()
    if (
        any(k in lower for k in ("prepare", "sop", "manual", "guide", "documentation", "how should"))
        and "search_documentation" not in portal_tools
        and not portal_tools
    ):
        skip_rag = False
    if not portal_tools and any(
        k in lower for k in ("what is", "what's", "whats", "define", "explain", "difference between")
    ):
        skip_rag = False

    if skip_rag:
        retrieval = rag_svc.RetrievalResult(
            citations=[],
            context_block="",
            intent=intent,
            low_confidence=False,
            latency_ms=0,
        )
    else:
        retrieval = rag_svc.retrieve(
            query=text,
            role_bucket=ctx.role_bucket,
            department_id=ctx.department_id,
            user=user,
            conversation=conversation,
            limit=3,
        )
    citations = retrieval.citations
    system = build_system_prompt(ctx)
    system = append_portal_context(system, portal_block=grounding.get("block") or "")
    if not skip_rag:
        system = append_retrieval_context(
            system,
            context_block=retrieval.context_block,
            citations=citations,
        )
    llm_messages = build_messages_for_llm(system_prompt=system, history=prior, user_message=text)
    gateway = get_gateway()
    from iic_booking.research_copilot.models import AuditAction
    from iic_booking.research_copilot.services.inference_concurrency import (
        BUSY_USER_MESSAGE,
        CopilotBusyError,
        acquire_generation_slot,
    )

    audit_svc.write_audit(
        action=AuditAction.STREAM_STARTED,
        message="Stream started",
        user=user,
        conversation=conversation,
    )

    pieces: list[str] = []
    try:
        with acquire_generation_slot(wait=False):
            for delta in gateway.stream(llm_messages, max_tokens=default_max_tokens()):
                pieces.append(delta)
                yield {"event": "delta", "data": {"text": delta}}
    except CopilotBusyError:
        yield {"event": "delta", "data": {"text": BUSY_USER_MESSAGE}}
        pieces = [BUSY_USER_MESSAGE]

    raw = "".join(pieces).strip() or (
        "Research Copilot is temporarily unavailable. "
        "Your booking and other portal operations are unaffected.\n" + ESCALATE_MARKER
    )
    reply, escalate = _strip_escalate(raw)
    reply = _append_sources_footer(reply, citations)
    reply = strip_internal_infra(reply)
    confidence = _estimate_confidence(
        escalate=escalate,
        provider="stream",
        text=reply,
        retrieval_low=retrieval.low_confidence,
        hit_count=len(citations) + len(grounding.get("tool_results") or []),
    )
    if confidence < CONFIDENCE_ESCALATE_THRESHOLD or retrieval.low_confidence:
        escalate = True

    base_actions = _static_actions(escalate=escalate)
    for a in reversed(grounding.get("actions") or []):
        if a.get("id") and all(x.get("id") != a.get("id") for x in base_actions):
            base_actions.insert(0, a)

    assistant = Message.objects.create(
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        content=reply,
        confidence=confidence,
        citations=rag_svc.citations_as_dicts(citations),
        suggested_actions=tools_svc.enrich_actions_from_message(
            user=user,
            text=text,
            base_actions=base_actions,
        ),
        escalate_hint=escalate,
        metadata={
            "streamed": True,
            "intent": retrieval.intent,
            "portal_tools": grounding.get("tool_results") or [],
            "response_modes": grounding.get("modes") or [],
        },
    )
    if not conversation.title or conversation.title == "New conversation":
        conversation.title = text[:80]
        conversation.save(update_fields=["title", "updated_at"])
    else:
        conversation.save(update_fields=["updated_at"])

    if escalate:
        KnowledgeGap.objects.create(
            conversation=conversation,
            user=user,
            query_summary=text[:512],
            reason="escalate_hint_stream",
        )

    yield {
        "event": "done",
        "data": {
            "message": serialize_message(assistant),
            "suggested_prompts": _suggested_for(ctx),
        },
    }


def add_feedback(*, user, conversation: Conversation, rating: str, comment: str = "", message_id=None) -> MessageFeedback:
    msg = None
    if message_id:
        msg = Message.objects.filter(id=message_id, conversation=conversation).first()
    fb = MessageFeedback.objects.create(
        conversation=conversation,
        message=msg,
        user=user,
        rating=rating,
        comment=(comment or "")[:2000],
    )
    from iic_booking.research_copilot.models import AuditAction

    audit_svc.write_audit(
        action=AuditAction.FEEDBACK,
        message=f"Feedback {rating}",
        user=user,
        conversation=conversation,
        detail={"rating": rating},
    )
    return fb


def serialize_message(m: Message) -> dict:
    return {
        "id": str(m.id),
        "role": m.role,
        "content": m.content,
        "confidence": m.confidence,
        "citations": m.citations or [],
        "suggested_actions": m.suggested_actions or [],
        "escalate_hint": bool(m.escalate_hint),
        "created_at": m.created_at.isoformat() if m.created_at else None,
        # Provider metrics (AI.17) — no secrets; used by UI/admin diagnostics
        "metadata": m.metadata or {},
    }


def serialize_conversation(c: Conversation, *, include_messages: bool = False) -> dict:
    data = {
        "id": str(c.id),
        "title": c.title,
        "access_mode": getattr(c, "access_mode", None) or "authenticated",
        "user_role_snapshot": c.user_role_snapshot,
        "department_id_snapshot": c.department_id_snapshot,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
    if include_messages:
        data["messages"] = [serialize_message(m) for m in c.messages.order_by("created_at")]
    return data


def bootstrap_payload(*, user=None) -> dict:
    from iic_booking.research_copilot.services.access_control import public_bootstrap_prompts
    from iic_booking.research_copilot.services.llm_gateway import configured_provider_name

    ctx = build_context(user)
    mode = effective_access_mode(user=user)
    full = authenticated_full_access(user=user)
    prompts = public_bootstrap_prompts() if mode == "public" else _suggested_for(ctx)
    command_actions = [
        {"id": "find_equipment", "label": "Find equipment", "href": "/equipments", "prompt": "Help me find suitable equipment for my sample."},
        {"id": "estimate_cost", "label": "Estimate booking cost", "prompt": "How much does 5 XRD samples cost?"},
        {"id": "research_help", "label": "Research Help", "prompt": "What is PXRD?"},
        {"id": "software", "label": "Find Analysis Software", "prompt": "What software is used for PXRD analysis?"},
    ]
    if full:
        command_actions = [
            {"id": "next_booking", "label": "My next booking", "prompt": "What is my next booking?"},
            {"id": "my_bookings", "label": "My bookings", "href": "/my-bookings", "prompt": "List my recent bookings."},
            {"id": "booking_status", "label": "Check booking status", "prompt": "What is the status of my latest booking?"},
            {"id": "sample_status", "label": "Check sample status", "prompt": "What is the sample status of my latest booking?"},
            {"id": "results", "label": "Check results", "prompt": "Are results available for my latest completed booking?"},
            {"id": "find_equipment", "label": "Find equipment", "href": "/equipments", "prompt": "Help me find suitable equipment for my sample."},
            {"id": "search_slots", "label": "Search available slots", "prompt": "Search available slots for FESEM this week."},
            {"id": "estimate_cost", "label": "Estimate booking cost", "prompt": "Estimate the cost of booking FESEM for 2 hours."},
            {"id": "software", "label": "Find Analysis Software", "href": "/remote-analysis/software-catalog"},
            {"id": "research_help", "label": "Research Help", "prompt": "How do I prepare a sample for FESEM?"},
        ]
    # Ordinary users see provider family only — no base URL / secrets.
    return {
        "enabled": feature_enabled(user=user),
        "assistant_name": "IIC Research Copilot",
        "access_mode": mode,
        "login_required_for_private": mode != "authenticated",
        "login_href": "/login",
        "public_banner": (
            "Ask about equipment, services, sample preparation, pricing and research facilities. "
            "Sign in to ask about your bookings, samples, results, wallet or Remote Analysis."
            if mode != "authenticated"
            else ""
        ),
        "role_bucket": ctx.role_bucket,
        "suggested_prompts": prompts,
        "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket, access_mode=mode),
        "capabilities": ctx.capabilities,
        "llm_provider": configured_provider_name(),
        "command_actions": command_actions,
    }
