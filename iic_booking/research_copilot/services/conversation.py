"""Conversation orchestration for IIC Research Copilot."""

from __future__ import annotations

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

    Optional pilot allowlist: RESEARCH_COPILOT_PILOT_EMAILS (comma-separated).
    When the allowlist is non-empty, only those emails may use Copilot while the
    global flag is true. Empty allowlist = all authenticated users (global).
    """
    if not bool(getattr(settings, "RESEARCH_COPILOT_ENABLED", False)):
        return False
    raw = (getattr(settings, "RESEARCH_COPILOT_PILOT_EMAILS", None) or "").strip()
    if not raw:
        return True
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not allowed:
        return True
    if user is None:
        return False
    email = (getattr(user, "email", None) or "").strip().lower()
    return email in allowed


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


def create_conversation(*, user, title: str = "") -> Conversation:
    ctx = build_context(user)
    conv = Conversation.objects.create(
        user=user,
        title=(title or "New conversation")[:255],
        user_role_snapshot=ctx.user_type[:64],
        department_id_snapshot=ctx.department_id,
    )
    audit_svc.audit_conversation_created(user=user, conversation=conv)
    return conv


def list_conversations(*, user, limit: int = 50):
    return Conversation.objects.filter(user=user, is_archived=False)[:limit]


def get_conversation(*, user, conversation_id) -> Conversation:
    return Conversation.objects.get(id=conversation_id, user=user)


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

    grounding = run_portal_grounding(user=user, text=text)

    retrieval = rag_svc.retrieve(
        query=text,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
        user=user,
        conversation=conversation,
    )
    citations = retrieval.citations
    system = build_system_prompt(ctx)
    system = append_portal_context(system, portal_block=grounding.get("block") or "")
    system = append_retrieval_context(
        system,
        context_block=retrieval.context_block,
        citations=citations,
    )

    llm_messages = build_messages_for_llm(system_prompt=system, history=prior, user_message=text)
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
                "llm_latency_ms": getattr(result, "latency_ms", 0) if result else 0,
                "llm_error_category": getattr(result, "error_category", "") if result else "",
                "prompt_tokens": getattr(result, "prompt_tokens", None) if result else None,
                "completion_tokens": getattr(result, "completion_tokens", None) if result else None,
                "portal_tools": grounding.get("tool_results") or [],
                "response_modes": grounding.get("modes") or [],
                "busy": busy,
            },
        )

        if not conversation.title or conversation.title == "New conversation":
            conversation.title = text[:80]
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["title", "updated_at"])

        if not busy and (escalate or retrieval.low_confidence):
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
        "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket),
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
    Message.objects.create(conversation=conversation, role=MessageRole.USER, content=text)
    prior = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages.order_by("created_at")
        if m.role in {MessageRole.USER, MessageRole.ASSISTANT}
    ][:-1]

    from iic_booking.research_copilot.services.portal_grounding import run_portal_grounding
    from iic_booking.research_copilot.services.prompt_builder import append_portal_context

    grounding = run_portal_grounding(user=user, text=text)

    retrieval = rag_svc.retrieve(
        query=text,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
        user=user,
        conversation=conversation,
    )
    citations = retrieval.citations
    system = build_system_prompt(ctx)
    system = append_portal_context(system, portal_block=grounding.get("block") or "")
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
            for delta in gateway.stream(llm_messages):
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
        "user_role_snapshot": c.user_role_snapshot,
        "department_id_snapshot": c.department_id_snapshot,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "updated_at": c.updated_at.isoformat() if c.updated_at else None,
    }
    if include_messages:
        data["messages"] = [serialize_message(m) for m in c.messages.order_by("created_at")]
    return data


def bootstrap_payload(*, user) -> dict:
    from iic_booking.research_copilot.services.llm_gateway import configured_provider_name

    ctx = build_context(user)
    # Ordinary users see provider family only — no base URL / secrets.
    return {
        "enabled": feature_enabled(user=user),
        "assistant_name": "IIC Research Copilot",
        "role_bucket": ctx.role_bucket,
        "suggested_prompts": _suggested_for(ctx),
        "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket),
        "capabilities": ctx.capabilities,
        "llm_provider": configured_provider_name(),
        "command_actions": [
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
        ],
    }
