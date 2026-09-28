"""
HTTP API for the Copilot intelligence layer:
  - user escalation to a support ticket (reuses the existing Ticket system)
  - knowledge article management with approval + versioning
  - admin console: unanswered questions, escalations, feedback, usage
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.research_copilot.models import (
    CopilotEscalation,
    CopilotKnowledgeArticle,
    Conversation,
    FeedbackRating,
    KnowledgeArticleAudience,
    KnowledgeArticleCategory,
    KnowledgeArticleStatus,
    KnowledgeGap,
    Message,
    MessageFeedback,
    MessageRole,
)
from iic_booking.research_copilot.services import conversation as conv_svc
from iic_booking.research_copilot.services.intelligence import intelligence_enabled, knowledge_enabled
from iic_booking.research_copilot.services.intelligence import articles as articles_svc
from iic_booking.research_copilot.services.intelligence import support as support_svc
from iic_booking.research_copilot.throttles import ResearchCopilotMutationThrottle, ResearchCopilotUserThrottle

MAX_ROWS = 200


def _error(code: str, message: str, http_status: int):
    return Response({"error": {"code": code, "message": message}}, status=http_status)


def _disabled():
    return _error("research_copilot_disabled", "IIC Booking Assistant is not enabled on this environment.", 503)


def _editor_gate(user):
    if not conv_svc.feature_enabled(user=user):
        return _disabled()
    if not articles_svc.can_edit(user):
        return _error("forbidden", "You cannot manage Copilot knowledge.", 403)
    return None


def _console_gate(user):
    """Unanswered questions, escalations, feedback and usage contain user questions: approvers only."""
    if not conv_svc.feature_enabled(user=user):
        return _disabled()
    if not articles_svc.can_approve(user):
        return _error("forbidden", "Only administrators can view Copilot analytics.", 403)
    return None


def _uuid_or_none(raw):
    try:
        return uuid.UUID(str(raw)) if raw else None
    except ValueError:
        return None


def _limit(request, default: int = 100) -> int:
    try:
        return max(1, min(MAX_ROWS, int(request.query_params.get("limit") or default)))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------------------------
# Escalation
# --------------------------------------------------------------------------------------------


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotMutationThrottle])
def conversation_escalate(request, conversation_id):
    if not conv_svc.feature_enabled(user=request.user):
        return _disabled()
    if not (intelligence_enabled() or knowledge_enabled()):
        return _error("escalation_disabled", "Booking Assistant escalation is not enabled.", 404)
    conv = get_object_or_404(Conversation, id=conversation_id, user=request.user)

    assistant_msg = None
    message_id = _uuid_or_none(request.data.get("message_id"))
    if message_id:
        assistant_msg = Message.objects.filter(
            id=message_id, conversation=conv, role=MessageRole.ASSISTANT
        ).first()
    before = Message.objects.filter(conversation=conv, role=MessageRole.USER)
    if assistant_msg is not None:
        before = before.filter(created_at__lte=assistant_msg.created_at)
    last_user = before.order_by("-created_at").first()

    meta = dict(getattr(assistant_msg, "metadata", None) or {})
    question = (
        str(meta.get("question") or "").strip()
        or (last_user.content if last_user else "")
        or str(request.data.get("question") or "")
    )
    state = conv.state if isinstance(conv.state, dict) else {}
    article = None
    article_id = _uuid_or_none(meta.get("knowledge_article_id"))
    if article_id:
        article = CopilotKnowledgeArticle.objects.filter(pk=article_id).first()

    try:
        result = support_svc.escalate(
            user=request.user,
            conversation=conv,
            question=question,
            reason=str(request.data.get("reason") or "user_requested"),
            intent=str(meta.get("intent") or state.get("last_intent") or ""),
            entities=meta.get("entities") if isinstance(meta.get("entities"), dict) else {},
            equipment_id=state.get("equipment_id") or state.get("last_equipment_id"),
            booking_id=state.get("booking_id"),
            copilot_response=assistant_msg.content if assistant_msg else "",
            message=assistant_msg,
            knowledge_article=article,
            note=str(request.data.get("note") or ""),
        )
    except support_svc.EscalationError as exc:
        return _error("escalation_failed", str(exc), getattr(exc, "status", 400))

    tid = result["ticket_id"]
    message = (
        f"Support ticket #{tid} already exists for this question."
        if result.get("duplicate")
        else f"Support ticket #{tid} has been created."
    )
    return Response(
        {**result, "message": message},
        status=status.HTTP_200_OK if result.get("duplicate") else status.HTTP_201_CREATED,
    )


# --------------------------------------------------------------------------------------------
# Knowledge articles
# --------------------------------------------------------------------------------------------


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def articles_collection(request):
    gated = _editor_gate(request.user)
    if gated:
        return gated
    if request.method == "POST":
        try:
            article = articles_svc.create(user=request.user, data=request.data)
        except articles_svc.ArticleError as exc:
            return _error("invalid_article", str(exc), exc.status)
        return Response(articles_svc.serialize(article, detail=True), status=status.HTTP_201_CREATED)

    qs = CopilotKnowledgeArticle.objects.select_related("created_by", "updated_by", "approved_by")
    st = (request.query_params.get("status") or "").strip()
    if st and st != "all":
        qs = qs.filter(status=st)
    cat = (request.query_params.get("category") or "").strip()
    if cat and cat != "all":
        qs = qs.filter(category=cat)
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(title__icontains=q) | Q(question__icontains=q) | Q(answer__icontains=q))
    rows = list(qs.order_by("-updated_at")[: _limit(request)])
    return Response(
        {
            "count": len(rows),
            "results": [articles_svc.serialize(a) for a in rows],
            "can_approve": articles_svc.can_approve(request.user),
            "categories": [{"value": v, "label": str(lbl)} for v, lbl in KnowledgeArticleCategory.choices],
            "audiences": [{"value": v, "label": str(lbl)} for v, lbl in KnowledgeArticleAudience.choices],
            "statuses": [{"value": v, "label": str(lbl)} for v, lbl in KnowledgeArticleStatus.choices],
        }
    )


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def article_detail(request, article_id):
    gated = _editor_gate(request.user)
    if gated:
        return gated
    article = get_object_or_404(CopilotKnowledgeArticle, pk=article_id)
    if request.method == "PATCH":
        try:
            article = articles_svc.update(user=request.user, article=article, data=request.data)
        except articles_svc.ArticleError as exc:
            return _error("invalid_article", str(exc), exc.status)
    return Response(articles_svc.serialize(article, detail=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotMutationThrottle])
def article_approve(request, article_id):
    gated = _editor_gate(request.user)
    if gated:
        return gated
    article = get_object_or_404(CopilotKnowledgeArticle, pk=article_id)
    try:
        article = articles_svc.approve(user=request.user, article=article)
    except articles_svc.ArticleError as exc:
        return _error("forbidden", str(exc), exc.status)
    return Response(articles_svc.serialize(article, detail=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotMutationThrottle])
def article_deactivate(request, article_id):
    gated = _editor_gate(request.user)
    if gated:
        return gated
    article = get_object_or_404(CopilotKnowledgeArticle, pk=article_id)
    try:
        article = articles_svc.deactivate(user=request.user, article=article)
    except articles_svc.ArticleError as exc:
        return _error("forbidden", str(exc), exc.status)
    return Response(articles_svc.serialize(article, detail=True))


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotMutationThrottle])
def article_from_ticket(request, ticket_id):
    """GET returns a prefilled draft; POST saves it (pending approval unless an approver publishes)."""
    from iic_booking.support.models import Ticket
    from iic_booking.support.ticket_service import user_can_access_ticket, user_can_manage_tickets

    gated = _editor_gate(request.user)
    if gated:
        return gated
    ticket = get_object_or_404(Ticket, pk=ticket_id)
    allowed = user_can_access_ticket(request.user, ticket) and (
        user_can_manage_tickets(request.user) or ticket.assigned_to_id == request.user.pk
    )
    if not allowed:
        return _error("forbidden", "You cannot use this ticket.", 403)

    if request.method == "GET":
        return Response(
            {
                "title": (ticket.subject or "")[:255],
                "question": ticket.description or ticket.subject or "",
                "answer": ticket.resolution_notes or "",
                "category": articles_svc._category_for_ticket_type(ticket.ticket_type),
                "audience": "all",
                "related_equipment_ids": [ticket.related_equipment_id] if ticket.related_equipment_id else [],
                "can_approve": articles_svc.can_approve(request.user),
            }
        )
    try:
        article = articles_svc.from_ticket(user=request.user, ticket=ticket, data=request.data)
    except articles_svc.ArticleError as exc:
        return _error("invalid_article", str(exc), exc.status)
    return Response(articles_svc.serialize(article, detail=True), status=status.HTTP_201_CREATED)


# --------------------------------------------------------------------------------------------
# Admin console
# --------------------------------------------------------------------------------------------


def _user_label(user) -> str | None:
    if user is None:
        return None
    return getattr(user, "email", None) or str(user.pk)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def unanswered_list(request):
    gated = _console_gate(request.user)
    if gated:
        return gated
    st = (request.query_params.get("status") or "open").strip()
    qs = KnowledgeGap.objects.select_related("user", "resolved_article")
    if st != "all":
        qs = qs.filter(status=st)
    rows = list(qs.order_by("-created_at")[: _limit(request)])
    return Response(
        {
            "count": len(rows),
            "results": [
                {
                    "id": str(g.id),
                    "question": g.query_summary,
                    "reason": g.reason,
                    "intent": g.intent,
                    "status": g.status,
                    "user": _user_label(g.user),
                    "conversation_id": str(g.conversation_id) if g.conversation_id else None,
                    "resolved_article_id": str(g.resolved_article_id) if g.resolved_article_id else None,
                    "created_at": g.created_at.isoformat() if g.created_at else None,
                }
                for g in rows
            ],
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotMutationThrottle])
def unanswered_resolve(request, gap_id):
    """Body: {"dismiss": true} or {"article_id": "..."} (link an existing article) or article fields (create)."""
    gated = _console_gate(request.user)
    if gated:
        return gated
    gap = get_object_or_404(KnowledgeGap, pk=gap_id)
    try:
        if request.data.get("dismiss"):
            articles_svc.resolve_gap(user=request.user, gap=gap, dismiss=True)
            return Response({"id": str(gap.id), "status": gap.status})
        article = None
        article_id = _uuid_or_none(request.data.get("article_id"))
        if article_id:
            article = get_object_or_404(CopilotKnowledgeArticle, pk=article_id)
        else:
            data = dict(request.data.items())
            data.setdefault("question", gap.query_summary)
            data.setdefault("title", gap.query_summary[:255])
            article = articles_svc.create(user=request.user, data=data, source="gap")
        articles_svc.resolve_gap(user=request.user, gap=gap, article=article)
    except articles_svc.ArticleError as exc:
        return _error("invalid_article", str(exc), exc.status)
    return Response({"id": str(gap.id), "status": gap.status, "article": articles_svc.serialize(article)})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def escalations_list(request):
    gated = _console_gate(request.user)
    if gated:
        return gated
    rows = list(
        CopilotEscalation.objects.select_related("ticket", "user").order_by("-created_at")[: _limit(request)]
    )
    return Response(
        {
            "count": len(rows),
            "results": [
                {
                    "id": str(e.id),
                    "ticket_id": e.ticket_id,
                    "ticket_status": getattr(e.ticket, "status", None),
                    "ticket_href": support_svc.ticket_href(e.ticket_id) if e.ticket_id else None,
                    "question": e.question,
                    "intent": e.intent,
                    "reason": e.reason,
                    "user": _user_label(e.user),
                    "equipment_id": e.equipment_id,
                    "booking_id": e.booking_id,
                    "created_at": e.created_at.isoformat() if e.created_at else None,
                }
                for e in rows
            ],
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def feedback_list(request):
    gated = _console_gate(request.user)
    if gated:
        return gated
    qs = MessageFeedback.objects.select_related("user", "message", "knowledge_article")
    rating = (request.query_params.get("rating") or "down").strip()
    if rating in {FeedbackRating.UP, FeedbackRating.DOWN}:
        qs = qs.filter(rating=rating)
    rows = list(qs.order_by("-created_at")[: _limit(request)])
    return Response(
        {
            "count": len(rows),
            "results": [
                {
                    "id": str(f.id),
                    "rating": f.rating,
                    "reason": f.reason,
                    "comment": f.comment,
                    "intent": f.intent,
                    "answer_excerpt": (f.message.content[:300] if f.message else ""),
                    "knowledge_article_id": str(f.knowledge_article_id) if f.knowledge_article_id else None,
                    "knowledge_article_title": getattr(f.knowledge_article, "title", None),
                    "user": _user_label(f.user),
                    "conversation_id": str(f.conversation_id),
                    "created_at": f.created_at.isoformat() if f.created_at else None,
                }
                for f in rows
            ],
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def usage_stats(request):
    gated = _console_gate(request.user)
    if gated:
        return gated
    try:
        days = max(1, min(365, int(request.query_params.get("days") or 30)))
    except (TypeError, ValueError):
        days = 30
    since = timezone.now() - timedelta(days=days)
    assistant = Message.objects.filter(role=MessageRole.ASSISTANT, created_at__gte=since)
    by_intent: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for meta in assistant.values_list("metadata", flat=True).iterator():
        if not isinstance(meta, dict):
            continue
        intent = str(meta.get("intent") or "legacy")
        mtype = str(meta.get("message_type") or meta.get("response_kind") or "text")
        by_intent[intent] = by_intent.get(intent, 0) + 1
        by_type[mtype] = by_type.get(mtype, 0) + 1
    feedback = MessageFeedback.objects.filter(created_at__gte=since)
    reasons = dict(
        feedback.filter(rating=FeedbackRating.DOWN)
        .exclude(reason="")
        .values_list("reason")
        .annotate(n=Count("id"))
        .values_list("reason", "n")
    )
    top_articles = list(
        CopilotKnowledgeArticle.objects.filter(usage_count__gt=0)
        .order_by("-usage_count")[:10]
        .values("id", "title", "usage_count", "helpful_count", "not_helpful_count")
    )
    return Response(
        {
            "days": days,
            "conversations": Conversation.objects.filter(created_at__gte=since, user__isnull=False).count(),
            "user_messages": Message.objects.filter(role=MessageRole.USER, created_at__gte=since).count(),
            "assistant_messages": sum(by_type.values()),
            "by_intent": dict(sorted(by_intent.items(), key=lambda kv: -kv[1])[:25]),
            "by_message_type": dict(sorted(by_type.items(), key=lambda kv: -kv[1])),
            "feedback_up": feedback.filter(rating=FeedbackRating.UP).count(),
            "feedback_down": feedback.filter(rating=FeedbackRating.DOWN).count(),
            "feedback_reasons": reasons,
            "escalations": CopilotEscalation.objects.filter(created_at__gte=since).count(),
            "open_unanswered": KnowledgeGap.objects.filter(status="open").count(),
            "articles": {
                "approved": CopilotKnowledgeArticle.objects.filter(status=KnowledgeArticleStatus.APPROVED).count(),
                "pending": CopilotKnowledgeArticle.objects.filter(
                    status=KnowledgeArticleStatus.PENDING_APPROVAL
                ).count(),
            },
            "top_articles": [{**a, "id": str(a["id"])} for a in top_articles],
        }
    )
