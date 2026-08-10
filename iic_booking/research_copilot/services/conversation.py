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
from iic_booking.research_copilot.services.llm_gateway import get_gateway
from iic_booking.research_copilot.services.prompt_builder import (
    append_retrieval_context,
    build_messages_for_llm,
    build_system_prompt,
)
from iic_booking.research_copilot.services import rag as rag_svc
from iic_booking.research_copilot.services import tools as tools_svc


def feature_enabled() -> bool:
    return bool(getattr(settings, "RESEARCH_COPILOT_ENABLED", False))


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
        {"id": "open_equipments", "label": "Open Equipments", "href": "/equipments", "enabled": True},
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
                "hint": "AI.5 will auto-create with conversation attached",
            },
        )
    # Future action cards (disabled)
    actions.append(
        {
            "id": "book_equipment",
            "label": "Book equipment",
            "enabled": False,
            "hint": "Requires AI.4 action execution + confirmation",
        }
    )
    return actions


@transaction.atomic
def send_message(*, user, conversation: Conversation, content: str) -> dict:
    text = (content or "").strip()
    if not text:
        raise ValueError("empty_message")

    ctx = build_context(user)
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
    # history includes the new user message as last — pass prior only
    prior = history[:-1]

    retrieval = rag_svc.retrieve(
        query=text,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
        user=user,
        conversation=conversation,
    )
    citations = retrieval.citations
    system = append_retrieval_context(
        build_system_prompt(ctx),
        context_block=retrieval.context_block,
        citations=citations,
    )

    llm_messages = build_messages_for_llm(system_prompt=system, history=prior, user_message=text)
    gateway = get_gateway()
    result = gateway.complete(llm_messages)
    raw = (result.text if result else "") or (
        "I could not generate a reply right now. Please try again or open **Tickets** for human support.\n"
        + ESCALATE_MARKER
    )
    reply, escalate = _strip_escalate(raw)
    reply = _append_sources_footer(reply, citations)
    provider = result.provider if result else "none"
    confidence = _estimate_confidence(
        escalate=escalate,
        provider=provider,
        text=reply,
        retrieval_low=retrieval.low_confidence,
        hit_count=len(citations),
    )
    if confidence < CONFIDENCE_ESCALATE_THRESHOLD or retrieval.low_confidence:
        escalate = True

    assistant = Message.objects.create(
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        content=reply,
        confidence=confidence,
        citations=rag_svc.citations_as_dicts(citations),
        suggested_actions=tools_svc.enrich_actions_from_message(
            user=user,
            text=text,
            base_actions=_static_actions(escalate=escalate),
        ),
        escalate_hint=escalate,
        metadata={
            "provider": provider,
            "model": result.model if result else "",
            "intent": retrieval.intent,
            "retrieval_latency_ms": retrieval.latency_ms,
        },
    )

    if not conversation.title or conversation.title == "New conversation":
        conversation.title = text[:80]
    conversation.updated_at = timezone.now()
    conversation.save(update_fields=["title", "updated_at"])

    if escalate or retrieval.low_confidence:
        KnowledgeGap.objects.create(
            conversation=conversation,
            user=user,
            query_summary=text[:512],
            reason="escalate_hint" if escalate else "low_retrieval",
            suggested_faq=f"Q: {text[:200]}\nA: (needs documentation)",
        )

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

    ctx = build_context(user)
    Message.objects.create(conversation=conversation, role=MessageRole.USER, content=text)
    prior = [
        {"role": m.role, "content": m.content}
        for m in conversation.messages.order_by("created_at")
        if m.role in {MessageRole.USER, MessageRole.ASSISTANT}
    ][:-1]

    retrieval = rag_svc.retrieve(
        query=text,
        role_bucket=ctx.role_bucket,
        department_id=ctx.department_id,
        user=user,
        conversation=conversation,
    )
    citations = retrieval.citations
    system = append_retrieval_context(
        build_system_prompt(ctx),
        context_block=retrieval.context_block,
        citations=citations,
    )
    llm_messages = build_messages_for_llm(system_prompt=system, history=prior, user_message=text)
    gateway = get_gateway()
    from iic_booking.research_copilot.models import AuditAction

    audit_svc.write_audit(
        action=AuditAction.STREAM_STARTED,
        message="Stream started",
        user=user,
        conversation=conversation,
    )

    pieces: list[str] = []
    for delta in gateway.stream(llm_messages):
        pieces.append(delta)
        yield {"event": "delta", "data": {"text": delta}}

    raw = "".join(pieces).strip() or (
        "I could not stream a reply. Please try again.\n" + ESCALATE_MARKER
    )
    reply, escalate = _strip_escalate(raw)
    reply = _append_sources_footer(reply, citations)
    confidence = _estimate_confidence(
        escalate=escalate,
        provider="stream",
        text=reply,
        retrieval_low=retrieval.low_confidence,
        hit_count=len(citations),
    )
    if confidence < CONFIDENCE_ESCALATE_THRESHOLD or retrieval.low_confidence:
        escalate = True

    assistant = Message.objects.create(
        conversation=conversation,
        role=MessageRole.ASSISTANT,
        content=reply,
        confidence=confidence,
        citations=rag_svc.citations_as_dicts(citations),
        suggested_actions=tools_svc.enrich_actions_from_message(
            user=user,
            text=text,
            base_actions=_static_actions(escalate=escalate),
        ),
        escalate_hint=escalate,
        metadata={"streamed": True, "intent": retrieval.intent},
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
    ctx = build_context(user)
    return {
        "enabled": feature_enabled(),
        "assistant_name": "IIC Research Copilot",
        "role_bucket": ctx.role_bucket,
        "suggested_prompts": _suggested_for(ctx),
        "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket),
        "capabilities": ctx.capabilities,
    }
