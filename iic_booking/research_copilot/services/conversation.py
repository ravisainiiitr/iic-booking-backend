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
    When the allowlist is non-empty, only those emails may use authenticated Copilot
    while the global flag is true. Empty allowlist = all authenticated users (global).

    Anonymous/public mode additionally requires RESEARCH_COPILOT_PUBLIC_ENABLED, so a
    signed-in pilot never implicitly opens the assistant to signed-out visitors.
    """
    if not bool(getattr(settings, "RESEARCH_COPILOT_ENABLED", False)):
        return False
    if user is None or not getattr(user, "is_authenticated", True):
        return bool(getattr(settings, "RESEARCH_COPILOT_PUBLIC_ENABLED", False))
    raw = (getattr(settings, "RESEARCH_COPILOT_PILOT_EMAILS", None) or "").strip()
    if not raw:
        return True
    allowed = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not allowed:
        return True
    email = (getattr(user, "email", None) or "").strip().lower()
    return email in allowed


def _reply_from_llm_result(result, *, user_text: str = "") -> str:
    """Map gateway result to user-visible text without exposing stack traces."""
    text = (result.text if result else "") or ""
    if text.strip():
        return text
    # Prefer deterministic fallback guidance over a hard failure when Ollama is down.
    try:
        from iic_booking.research_copilot.services.llm_gateway import FallbackGateway

        fb = FallbackGateway().complete(
            [{"role": "user", "content": (user_text or "help")[:2000]}],
            max_tokens=400,
        )
        if fb and (fb.text or "").strip():
            return fb.text
    except Exception:  # noqa: BLE001
        pass
    category = getattr(result, "error_category", "") if result else ""
    if category:
        return (
            "Booking Assistant is temporarily unavailable. "
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


def list_conversations(*, user, limit: int = 50, archived: bool = False):
    from django.db.models import OuterRef, Subquery

    last_user_msg = (
        Message.objects.filter(conversation=OuterRef("pk"), role=MessageRole.USER).order_by("-created_at").values("content")[:1]
    )
    return Conversation.objects.filter(user=user, is_archived=archived).annotate(last_query=Subquery(last_user_msg))[:limit]


def archive_conversation(*, user, conversation_id, archived: bool = True) -> Conversation:
    conv = Conversation.objects.get(id=conversation_id, user=user)
    conv.is_archived = archived
    conv.save(update_fields=["is_archived", "updated_at"])
    return conv


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


def _reply_deterministic(*, user, conversation: Conversation, text: str, det: dict, ctx, enrich: bool = True) -> dict:
    reply = det.get("content") or ""
    actions = list(det.get("suggested_actions") or [])
    cards = list(det.get("cards") or [])
    escalate = bool(det.get("escalate_hint"))
    confidence = float(det.get("confidence") or 0.88)
    meta = dict(det.get("metadata") or {})
    title_hint = meta.pop("title_hint", None)
    title_defer = bool(meta.pop("title_defer", False))
    replace_title = meta.pop("replace_title", None)
    with transaction.atomic():
        assistant = Message.objects.create(
            conversation=conversation,
            role=MessageRole.ASSISTANT,
            content=reply,
            confidence=confidence,
            citations=list(meta.get("citations") or []),
            suggested_actions=(
                tools_svc.enrich_actions_from_message(user=user, text=text, base_actions=actions) if enrich else actions
            ),
            escalate_hint=escalate,
            metadata={
                **meta,
                "cards": cards,
                "response_kind": det.get("response_kind") or "LIVE_DATA",
                "llm_used": bool(meta.get("llm_used")),
                "v2": True,
            },
        )
        untitled = not conversation.title or conversation.title == "New conversation"
        if title_hint and replace_title and conversation.title == replace_title:
            conversation.title = title_hint[:80]
        elif untitled and not title_defer:
            conversation.title = (title_hint or text)[:80]
        conversation.updated_at = timezone.now()
        conversation.save(update_fields=["title", "updated_at"])
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
        "cards": cards,
        "response_kind": det.get("response_kind"),
    }


def send_message(
    *,
    user,
    conversation: Conversation,
    content: str,
    choice: dict | None = None,
    action: dict | None = None,
    assistant_action: dict | None = None,
) -> dict:
    """
    Persist user message, prefer deterministic V2 reads, else portal grounding + RAG + LLM.

    Critical path isolation (AI.17):
    - No long-lived DB transaction around Ollama/OpenAI.
    - Deterministic operational queries do not require the LLM.
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

    from iic_booking.research_copilot.services.intelligence import conversational_actions_enabled

    # Contextual actions only: no global navigation footer or keyword-derived buttons on any reply.
    contextual = conversational_actions_enabled()
    ctx = build_context(user)
    with transaction.atomic():
        Message.objects.create(
            conversation=conversation,
            role=MessageRole.USER,
            content=text,
            **(
                {"metadata": {"action": assistant_action or action}}
                if assistant_action or (action and contextual)
                else {}
            ),
        )

    # --- Booking Assistant: availability, equipment Q&A, confirm-gated booking over live data ---
    from iic_booking.research_copilot.services.assistant.engine import try_assistant_turn

    helper = try_assistant_turn(
        user=user,
        text=text,
        conversation=conversation,
        assistant_action=assistant_action,
        choice=choice,
        action=action,
    )
    if helper is not None:
        return _reply_deterministic(user=user, conversation=conversation, text=text, det=helper, ctx=ctx, enrich=False)

    # --- Intelligence layer (flagged): intents, choices, guided actions, verified knowledge ---
    from iic_booking.research_copilot.services.intelligence.engine import try_intelligent_turn

    smart = try_intelligent_turn(user=user, text=text, conversation=conversation, choice=choice, action=action)
    if smart is not None:
        return _reply_deterministic(user=user, conversation=conversation, text=text, det=smart, ctx=ctx, enrich=False)

    # --- Phase A: deterministic-first (no LLM) ---
    from iic_booking.research_copilot.services.v2.orchestrator import try_deterministic_turn

    det = try_deterministic_turn(user=user, text=text, conversation=conversation, public=False)
    if det is not None:
        return _reply_deterministic(user=user, conversation=conversation, text=text, det=det, ctx=ctx, enrich=not contextual)

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
    from iic_booking.research_copilot.throttles import consume_llm_quota

    llm_ok, llm_msg = consume_llm_quota(user=user)
    if not llm_ok:
        with transaction.atomic():
            assistant = Message.objects.create(
                conversation=conversation,
                role=MessageRole.ASSISTANT,
                content=llm_msg,
                confidence=0.5,
                citations=[],
                suggested_actions=[]
                if contextual
                else _static_actions(escalate=False)
                + [
                    {"id": "my_bookings", "label": "My bookings", "href": "/my-bookings", "enabled": True},
                    {"id": "equipments", "label": "Find equipment", "href": "/equipments", "enabled": True},
                ],
                escalate_hint=False,
                metadata={"llm_used": False, "llm_quota_blocked": True, "v2": True},
            )
            conversation.updated_at = timezone.now()
            conversation.save(update_fields=["updated_at"])
        return {
            "conversation_id": str(conversation.id),
            "message": serialize_message(assistant),
            "suggested_prompts": _suggested_for(ctx),
            "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket),
        }

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

    raw = _reply_from_llm_result(result, user_text=text)
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

    if contextual:
        from iic_booking.research_copilot.services.intelligence import messages as intel_messages

        reply_actions = [a for a in (grounding.get("actions") or []) if a.get("id")][:3]
        if escalate:
            reply_actions.append(intel_messages.ticket_action("no_verified_answer", "Raise Support Ticket"))
    else:
        base_actions = _static_actions(escalate=escalate)
        for a in reversed(grounding.get("actions") or []):
            if a.get("id") and all(x.get("id") != a.get("id") for x in base_actions):
                base_actions.insert(0, a)
        reply_actions = tools_svc.enrich_actions_from_message(user=user, text=text, base_actions=base_actions)

    with transaction.atomic():
        assistant = Message.objects.create(
            conversation=conversation,
            role=MessageRole.ASSISTANT,
            content=reply,
            confidence=confidence,
            citations=rag_svc.citations_as_dicts(citations) if not busy else [],
            suggested_actions=reply_actions,
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
                "llm_used": True,
                "v2": True,
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
    Yield SSE-ready dict events.

    Delegates to send_message so streaming gets exactly the same deterministic-first path,
    LLM quota, concurrency slot, persistence and audit as the regular endpoint. Local models
    return whole completions, so the reply is emitted as one delta followed by "done".
    """
    result = send_message(user=user, conversation=conversation, content=content)
    message = result.get("message") or {}
    yield {"event": "delta", "data": {"text": message.get("content") or ""}}
    yield {
        "event": "done",
        "data": {
            "message": message,
            "suggested_prompts": result.get("suggested_prompts") or [],
            "cards": result.get("cards") or [],
            "response_kind": result.get("response_kind"),
        },
    }


def add_feedback(
    *, user, conversation: Conversation, rating: str, comment: str = "", message_id=None, reason: str = ""
) -> MessageFeedback:
    from iic_booking.research_copilot.models import AuditAction, CopilotKnowledgeArticle, FeedbackReason

    msg = None
    if message_id:
        msg = Message.objects.filter(id=message_id, conversation=conversation).first()
    meta = (msg.metadata or {}) if msg is not None else {}
    reason = reason if reason in FeedbackReason.values else ""
    article = None
    article_id = meta.get("knowledge_article_id")
    if article_id:
        try:
            article = CopilotKnowledgeArticle.objects.filter(pk=article_id).first()
        except Exception:  # noqa: BLE001 - malformed ids in old metadata
            article = None
    fb = MessageFeedback.objects.create(
        conversation=conversation,
        message=msg,
        user=user,
        rating=rating,
        comment=(comment or "")[:2000],
        reason=reason,
        intent=str(meta.get("intent") or "")[:64],
        knowledge_article=article,
    )
    if article is not None:
        from iic_booking.research_copilot.services.intelligence import knowledge

        knowledge.record_feedback(article.pk, helpful=rating == "up")
    audit_svc.write_audit(
        action=AuditAction.FEEDBACK,
        message=f"Feedback {rating}",
        user=user,
        conversation=conversation,
        detail={"rating": rating, "reason": reason},
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
        "is_archived": bool(c.is_archived),
    }
    last_query = getattr(c, "last_query", None)
    if not hasattr(c, "last_query") and not include_messages:
        last_query = (
            c.messages.filter(role=MessageRole.USER).order_by("-created_at").values_list("content", flat=True).first()
        )
    if last_query is not None:
        data["last_query"] = str(last_query)[:160]
    if include_messages:
        data["messages"] = [serialize_message(m) for m in c.messages.order_by("created_at")]
    return data


def bootstrap_payload(*, user) -> dict:
    from django.conf import settings

    from iic_booking.research_copilot.services.llm_gateway import configured_provider_name

    ctx = build_context(user)
    # Ordinary users see provider family only — no base URL / secrets.
    return {
        "enabled": feature_enabled(user=user),
        "assistant_name": "IIC Booking Assistant",
        "role_bucket": ctx.role_bucket,
        "suggested_prompts": _suggested_for(ctx),
        "tools_available": tools_svc.list_tools_for_role(ctx.role_bucket),
        "capabilities": ctx.capabilities,
        "llm_provider": configured_provider_name(),
        "command_actions": [
            {"id": "ba_options", "label": "FESEM tomorrow?", "prompt": "I need FESEM tomorrow — what are my options?"},
            {"id": "ba_upcoming", "label": "Upcoming bookings", "prompt": "Show my upcoming bookings."},
            {"id": "ba_capability", "label": "Which instrument?", "prompt": "Which equipment can do x-ray diffraction?"},
            {"id": "ba_charges", "label": "Charges", "prompt": "What are the charges for XRD?"},
            {"id": "ba_cancel_rules", "label": "Cancellation rules", "prompt": "How do I cancel a booking?"},
            {"id": "find_equipment", "label": "Find equipment", "prompt": "Help me find suitable equipment for my sample."},
            {"id": "search_slots", "label": "Find available slots", "prompt": "Search available slots for FESEM this week."},
            {"id": "estimate_cost", "label": "Estimate cost", "prompt": "Estimate the cost of booking FESEM for 2 hours."},
            {"id": "my_bookings", "label": "My bookings", "prompt": "List my recent bookings."},
            {"id": "next_booking", "label": "Next booking", "prompt": "What is my next booking?"},
            {"id": "reschedule", "label": "Reschedule booking", "prompt": "Reschedule my next booking."},
            {"id": "cancel_booking", "label": "Cancel booking", "prompt": "Cancel my next booking."},
            {"id": "wallet", "label": "Wallet balance", "prompt": "What is my wallet balance?"},
            {"id": "wallet_tx", "label": "Wallet transactions", "prompt": "Show my recent wallet transactions."},
            {"id": "recharge", "label": "Recharge wallet", "prompt": "I want to recharge my wallet."},
            {"id": "credit", "label": "Credit status", "prompt": "What is my outstanding credit?"},
            {"id": "ra_status", "label": "Remote Analysis", "prompt": "What is my Remote Analysis status?"},
            {"id": "pending", "label": "Pending actions", "prompt": "What are my pending actions?"},
            {"id": "research_help", "label": "Research Help", "prompt": "How do I prepare a sample for FESEM?"},
        ],
        "intelligence": _intelligence_flags(user),
        "booking_assistant": {"enabled": _booking_assistant_enabled()},
        "command_groups": _command_groups(user),
        "mutation_flags": {
            "booking_create": _booking_flag_for_user(user, "COPILOT_BOOKING_CREATE"),
            "booking_cancel": _booking_flag_for_user(user, "COPILOT_BOOKING_CANCEL"),
            "booking_reschedule": _booking_flag_for_user(user, "COPILOT_BOOKING_RESCHEDULE"),
            "wallet_read": bool(getattr(settings, "COPILOT_WALLET_READ", True)),
            "wallet_recharge": bool(getattr(settings, "COPILOT_WALLET_RECHARGE", False)),
            "wallet_credit": bool(getattr(settings, "COPILOT_WALLET_CREDIT", False)),
            "financial_proposals": bool(getattr(settings, "COPILOT_FINANCIAL_PROPOSALS", False)),
            "invoice_read": bool(getattr(settings, "COPILOT_INVOICE_READ", True)),
            "financial_admin": bool(getattr(settings, "COPILOT_FINANCIAL_ADMIN", False)),
            "e2e_test_mode": bool(getattr(settings, "COPILOT_BOOKING_E2E_TEST_MODE", False)),
        },
    }


def _booking_assistant_enabled() -> bool:
    from iic_booking.research_copilot.services.assistant.engine import assistant_enabled

    return assistant_enabled()


def _intelligence_flags(user) -> dict:
    from iic_booking.research_copilot.services.intelligence import actions_enabled, intelligence_enabled, knowledge_enabled
    from iic_booking.research_copilot.services.intelligence.articles import can_approve, can_edit

    return {
        "enabled": intelligence_enabled(),
        "knowledge": knowledge_enabled(),
        "actions": actions_enabled(),
        "can_manage_knowledge": can_edit(user),
        "can_approve_knowledge": can_approve(user),
    }


def _command_groups(user) -> list[dict]:
    """Grouped quick actions. Choice buttons start the guided flows; prompts use the existing reads."""
    from iic_booking.research_copilot.services.intelligence import intelligence_enabled

    def start(value: str, label: str, fallback_prompt: str) -> dict:
        if intelligence_enabled():
            return {"id": f"start_{value}", "label": label, "choice": {"kind": "start", "value": value}}
        return {"id": f"start_{value}", "label": label, "prompt": fallback_prompt}

    groups = [
        {
            "id": "booking",
            "label": "Booking",
            "actions": [
                start("book", "Book equipment", "I want to book equipment."),
                start("availability", "Check availability", "Search available slots for FESEM this week."),
                start("estimate", "Estimate cost", "Estimate the cost of booking FESEM."),
                {"id": "my_bookings", "label": "My bookings", "prompt": "List my recent bookings."},
                {"id": "next_booking", "label": "Next booking", "prompt": "What is my next booking?"},
                start("cancel", "Cancel a booking", "Cancel my next booking."),
                start("reschedule", "Reschedule", "Reschedule my next booking."),
            ],
        },
        {
            "id": "research",
            "label": "Research",
            "actions": [
                {"id": "find_equipment", "label": "Find equipment", "prompt": "Which technique should I use for elemental composition?"},
                {"id": "results", "label": "My results", "prompt": "Show my latest results."},
                {"id": "ra_status", "label": "Remote Analysis", "prompt": "What is my Remote Analysis status?"},
                {"id": "research_help", "label": "Sample preparation", "prompt": "How do I prepare a sample for FESEM?"},
            ],
        },
        {
            "id": "account",
            "label": "Account",
            "actions": [
                {"id": "wallet", "label": "Wallet balance", "prompt": "What is my wallet balance?"},
                {"id": "wallet_tx", "label": "Wallet transactions", "prompt": "Show my recent wallet transactions."},
                {"id": "credit", "label": "Credit status", "prompt": "What is my outstanding credit?"},
                {"id": "pending", "label": "Pending actions", "prompt": "What are my pending actions?"},
            ],
        },
        {
            "id": "help",
            "label": "Help",
            "actions": [
                {"id": "portal_help", "label": "Portal help", "prompt": "How do I cancel a booking?"},
                {"id": "support", "label": "Contact support", "prompt": "I want to raise a support ticket."},
                {"id": "tickets", "label": "My tickets", "href": "/tickets"},
            ],
        },
    ]
    return groups


def _booking_flag_for_user(user, flag_name: str) -> bool:
    from iic_booking.research_copilot.services.v2.mutations import booking_mutation_allowed

    return booking_mutation_allowed(user, flag_name)


def public_bootstrap_payload() -> dict:
    """Anonymous bootstrap — no personal tools."""
    from iic_booking.research_copilot.services.llm_gateway import configured_provider_name

    enabled = feature_enabled(user=None)
    return {
        "enabled": enabled,
        "assistant_name": "IIC Booking Assistant",
        "role_bucket": "public",
        "suggested_prompts": [
            "What does HOLD mean on a booking?",
            "Search available slots for FESEM",
            "Estimate the cost of booking PXRD for educational institute users",
            "How do I accept a sample?",
            "Remote Analysis won't connect — troubleshoot.",
            "Where are operator manuals?",
        ],
        "tools_available": [
            {"name": n, "description": d, "mutating": False, "available": True}
            for n, d in (
                ("search_documentation", "Public FAQ / documentation"),
                ("search_equipment", "Search instruments"),
                ("search_slots", "Browse free slots (no booking)"),
                ("estimate_booking_cost", "Rough public charge estimate"),
            )
        ],
        "capabilities": ["ask_docs", "public_slots", "public_estimate", "equipment_advisor"],
        "llm_provider": configured_provider_name(),
        "auth_required_for": ["book", "wallet", "my_bookings", "recharge", "cancel"],
        "command_actions": [
            {"id": "hold_meaning", "label": "What is HOLD?", "prompt": "What does HOLD mean on a booking?"},
            {"id": "find_equipment", "label": "Find equipment", "href": "/equipments", "prompt": "Help me find suitable equipment for my sample."},
            {"id": "search_slots", "label": "Search available slots", "prompt": "Search available slots for FESEM this week."},
            {"id": "estimate_cost", "label": "Estimate booking cost", "prompt": "Estimate the cost of booking FESEM."},
            {"id": "sign_in", "label": "Sign in to book", "href": "/auth"},
            {"id": "research_help", "label": "Research Help", "prompt": "How do I prepare a sample for FESEM?"},
        ],
    }


def public_ask(*, text: str) -> dict:
    """
    One-shot anonymous ask (no conversation persistence / no schema change).

    Deterministic public reads first; else RAG + LLM (with fallback).
    """
    from iic_booking.equipment.print_3d_views import get_charge_estimate_guest_user
    from iic_booking.research_copilot.services.portal_grounding import run_portal_grounding
    from iic_booking.research_copilot.services.prompt_builder import append_portal_context
    from iic_booking.research_copilot.services.inference_concurrency import (
        BUSY_USER_MESSAGE,
        CopilotBusyError,
        acquire_generation_slot,
    )
    from iic_booking.research_copilot.services.v2.orchestrator import try_deterministic_turn

    text = (text or "").strip()
    if len(text) < 2:
        return {
            "ok": False,
            "error": "empty_message",
            "message": "Please enter a question.",
        }
    if len(text) > int(getattr(settings, "RESEARCH_COPILOT_MAX_INPUT_CHARS", 4000) or 4000):
        return {
            "ok": False,
            "error": "message_too_long",
            "message": "Message is too long.",
        }

    # Phase A: deterministic public reads (slots / equipment / estimate / docs) without LLM
    det = try_deterministic_turn(user=None, text=text, conversation=None, public=True)
    if det is not None:
        cards = list(det.get("cards") or [])
        return {
            "ok": True,
            "enabled": True,
            "cards": cards,
            "response_kind": det.get("response_kind"),
            "message": {
                "role": "assistant",
                "content": det.get("content") or "",
                "citations": list((det.get("metadata") or {}).get("citations") or []),
                "suggested_actions": list(det.get("suggested_actions") or []),
                "escalate_hint": bool(det.get("escalate_hint")),
                "metadata": {
                    **(det.get("metadata") or {}),
                    "cards": cards,
                    "public": True,
                    "llm_used": bool((det.get("metadata") or {}).get("llm_used")),
                    "v2": True,
                },
            },
        }

    guest = get_charge_estimate_guest_user()
    ctx = build_context(None)
    grounding = run_portal_grounding(user=guest, text=text, public=True)
    retrieval = rag_svc.retrieve(
        query=text,
        role_bucket="public",
        department_id=None,
        user=None,
        conversation=None,
    )
    citations = retrieval.citations
    system = build_system_prompt(ctx)
    system = append_portal_context(system, portal_block=grounding.get("block") or "")
    system = append_retrieval_context(
        system,
        context_block=retrieval.context_block,
        citations=citations,
    )
    system += (
        "\n\nYou are answering an anonymous visitor. "
        "You may discuss equipment, free slots, rough charge estimates, and documentation. "
        "If they ask to book, recharge wallet, or view personal bookings, tell them to Sign in."
    )

    llm_messages = build_messages_for_llm(system_prompt=system, history=[], user_message=text)
    gateway = get_gateway()
    result = None
    busy = False
    try:
        with acquire_generation_slot(wait=False):
            result = gateway.generate(llm_messages, max_tokens=default_max_tokens())
    except CopilotBusyError:
        busy = True
        result = type(
            "R",
            (),
            {
                "text": BUSY_USER_MESSAGE + "\n" + ESCALATE_MARKER,
                "provider": "none",
                "model": "",
                "error_category": "busy",
                "latency_ms": 0,
                "prompt_tokens": None,
                "completion_tokens": None,
            },
        )()

    raw = _reply_from_llm_result(result, user_text=text)
    reply, escalate = _strip_escalate(raw)
    if not busy:
        reply = _append_sources_footer(reply, citations)

    base_actions = list(grounding.get("actions") or [])
    # Always offer sign-in for privileged follow-ups
    if all(a.get("id") != "sign_in_cta" for a in base_actions):
        base_actions.append(
            {
                "id": "sign_in_cta",
                "label": "Sign in for booking & wallet",
                "href": "/auth",
                "enabled": True,
            }
        )

    return {
        "ok": True,
        "enabled": True,
        "message": {
            "role": "assistant",
            "content": reply,
            "citations": rag_svc.citations_as_dicts(citations) if not busy else [],
            "suggested_actions": base_actions,
            "escalate_hint": bool(escalate),
            "metadata": {
                "provider": getattr(result, "provider", "") if result else "",
                "public": True,
                "portal_tools": grounding.get("tool_results") or [],
                "busy": busy,
            },
        },
    }
