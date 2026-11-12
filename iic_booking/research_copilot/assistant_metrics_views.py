"""IITR Equipment Booking Assistant trial metrics for the Main Administrator: counts only, no message text."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from django.db.models import Count
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.research_copilot.knowledge_views import IsCopilotKnowledgeAdmin
from iic_booking.research_copilot.models import AuditAction, CopilotAuditEvent, MessageFeedback

MAX_DAYS = 180
TOP_INTENTS = 25


@api_view(["GET"])
@permission_classes([IsAuthenticated, IsCopilotKnowledgeAdmin])
def assistant_metrics(request):
    """GET ?days=30 : turns, recognised share, outcomes (clarification, action suggested, completed, refused,
    not found, unrecognised, failure), typed vs button, top intents, roles and thumbs up/down."""
    try:
        days = min(max(int(request.query_params.get("days") or 30), 1), MAX_DAYS)
    except (TypeError, ValueError):
        days = 30
    since = timezone.now() - timedelta(days=days)
    outcomes: Counter = Counter()
    sources: Counter = Counter()
    intents: Counter = Counter()
    roles: Counter = Counter()
    recognised = turns = 0
    rows = CopilotAuditEvent.objects.filter(action=AuditAction.ASSISTANT_TURN, created_at__gte=since)
    for detail in rows.values_list("detail", flat=True).iterator():
        detail = detail or {}
        turns += 1
        recognised += bool(detail.get("recognised"))
        outcomes[detail.get("outcome") or "unknown"] += 1
        sources[detail.get("source") or "typed"] += 1
        if detail.get("intent"):
            intents[detail["intent"]] += 1
        for role in detail.get("roles") or []:
            roles[role] += 1
    feedback = dict(
        MessageFeedback.objects.filter(created_at__gte=since).values_list("rating").annotate(n=Count("id"))
    )
    return Response({
        "days": days,
        "turns": turns,
        "recognised": recognised,
        "recognition_rate": round(recognised / turns, 3) if turns else None,
        "outcomes": dict(outcomes),
        "sources": dict(sources),
        "top_intents": [{"intent": k, "count": v} for k, v in intents.most_common(TOP_INTENTS)],
        "roles": dict(roles),
        "feedback": {"up": feedback.get("up", 0), "down": feedback.get("down", 0)},
    })
