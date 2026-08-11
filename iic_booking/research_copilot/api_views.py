"""HTTP API for IIC Research Copilot (Phase AI.1)."""

from __future__ import annotations

import json

from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.research_copilot.models import AuditAction, Conversation, FeedbackRating
from iic_booking.research_copilot.services import audit as audit_svc
from iic_booking.research_copilot.services import conversation as conv_svc
from iic_booking.research_copilot.services.context_builder import build_context
from iic_booking.research_copilot.constants import SUGGESTED_PROMPTS
from iic_booking.research_copilot.throttles import ResearchCopilotToolThrottle, ResearchCopilotUserThrottle


def _feature_gate(*, user=None, audit: bool = True):
    if not conv_svc.feature_enabled(user=user):
        if audit and user is not None:
            audit_svc.write_audit(
                action=AuditAction.FEATURE_DISABLED,
                message="Research Copilot feature flag disabled",
                user=user,
                detail={"endpoint": "gated"},
            )
        return Response(
            {
                "error": {
                    "code": "research_copilot_disabled",
                    "message": "IIC Research Copilot is not enabled on this environment.",
                }
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )
    return None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def bootstrap(request):
    """Public config for the Copilot UI (still requires auth)."""
    if not conv_svc.feature_enabled(user=request.user):
        # Still return bootstrap shape with enabled=false for UI to hide gracefully
        ctx = build_context(request.user)
        return Response(
            {
                "enabled": False,
                "assistant_name": "IIC Research Copilot",
                "role_bucket": ctx.role_bucket,
                "suggested_prompts": SUGGESTED_PROMPTS.get(ctx.role_bucket) or SUGGESTED_PROMPTS["default"],
                "tools_available": [],
                "capabilities": ctx.capabilities,
            }
        )
    return Response(conv_svc.bootstrap_payload(user=request.user))


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def conversations_collection(request):
    gated = _feature_gate(user=request.user)
    if gated:
        return gated

    if request.method == "GET":
        rows = conv_svc.list_conversations(user=request.user)
        return Response(
            {
                "count": len(rows),
                "results": [conv_svc.serialize_conversation(c) for c in rows],
            }
        )

    title = (request.data.get("title") or "").strip()
    conv = conv_svc.create_conversation(user=request.user, title=title)
    ctx = build_context(request.user)
    return Response(
        {
            "conversation": conv_svc.serialize_conversation(conv, include_messages=True),
            "suggested_prompts": SUGGESTED_PROMPTS.get(ctx.role_bucket) or SUGGESTED_PROMPTS["default"],
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def conversation_detail(request, conversation_id):
    gated = _feature_gate(user=request.user)
    if gated:
        return gated
    conv = get_object_or_404(Conversation, id=conversation_id, user=request.user)
    return Response(conv_svc.serialize_conversation(conv, include_messages=True))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def conversation_messages(request, conversation_id):
    gated = _feature_gate(user=request.user)
    if gated:
        return gated
    conv = get_object_or_404(Conversation, id=conversation_id, user=request.user)
    content = request.data.get("content") or request.data.get("message") or ""
    try:
        payload = conv_svc.send_message(user=request.user, conversation=conv, content=content)
    except ValueError as exc:
        return Response(
            {"error": {"code": str(exc), "message": "Invalid message."}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def conversation_messages_stream(request, conversation_id):
    gated = _feature_gate(user=request.user)
    if gated:
        return gated
    conv = get_object_or_404(Conversation, id=conversation_id, user=request.user)
    content = request.data.get("content") or request.data.get("message") or ""

    def event_stream():
        try:
            for item in conv_svc.stream_message_deltas(
                user=request.user,
                conversation=conv,
                content=content,
            ):
                ev = item.get("event", "message")
                data = json.dumps(item.get("data") or {})
                yield f"event: {ev}\ndata: {data}\n\n"
        except ValueError as exc:
            yield f"event: error\ndata: {json.dumps({'code': str(exc)})}\n\n"
        except Exception:
            yield f"event: error\ndata: {json.dumps({'code': 'stream_failed'})}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def conversation_feedback(request, conversation_id):
    gated = _feature_gate(user=request.user)
    if gated:
        return gated
    conv = get_object_or_404(Conversation, id=conversation_id, user=request.user)
    rating = (request.data.get("rating") or "").strip().lower()
    if rating not in {FeedbackRating.UP, FeedbackRating.DOWN}:
        return Response(
            {"error": {"code": "invalid_rating", "message": "rating must be up or down."}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    fb = conv_svc.add_feedback(
        user=request.user,
        conversation=conv,
        rating=rating,
        comment=request.data.get("comment") or "",
        message_id=request.data.get("message_id"),
    )
    return Response({"id": str(fb.id), "rating": fb.rating}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotToolThrottle])
def execute_tool(request):
    """Execute a Copilot tool (read-only or confirmation action-card)."""
    gated = _feature_gate(user=request.user)
    if gated:
        return gated
    from iic_booking.research_copilot.services import tools as tools_svc

    name = (request.data.get("name") or request.data.get("tool") or "").strip()
    arguments = request.data.get("arguments") or request.data.get("args") or {}
    if not name:
        return Response(
            {"error": {"code": "missing_tool", "message": "name is required"}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not isinstance(arguments, dict):
        return Response(
            {"error": {"code": "invalid_arguments", "message": "arguments must be an object"}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    result = tools_svc.execute_tool(name=name, arguments=arguments, user=request.user)
    code = status.HTTP_200_OK if result.get("ok") else status.HTTP_400_BAD_REQUEST
    return Response(result, status=code)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotUserThrottle])
def llm_provider_health(request):
    """
    Staff/admin LLM provider diagnostics (AI.17).

    Returns provider/model/status only — never API keys or internal URLs.
    """
    from iic_booking.research_copilot.knowledge_views import IsCopilotKnowledgeAdmin
    from iic_booking.research_copilot.services.llm_gateway import (
        configured_provider_name,
        openai_model_name,
        ollama_model_name,
        provider_health,
    )

    if not IsCopilotKnowledgeAdmin().has_permission(request, None):
        return Response(
            {"error": {"code": "forbidden", "message": "Admin access required."}},
            status=status.HTTP_403_FORBIDDEN,
        )
    health = provider_health()
    payload = health.as_public_dict()
    payload["configured_provider"] = configured_provider_name()
    # Model expected by config (not secrets)
    if configured_provider_name() == "openai":
        payload["configured_model"] = openai_model_name()
    elif configured_provider_name() == "ollama":
        payload["configured_model"] = ollama_model_name()
        payload["model_available"] = health.status == "available"
    # Boolean only — never the key value
    from django.conf import settings as dj_settings

    payload["openai_api_key_configured"] = bool((getattr(dj_settings, "OPENAI_API_KEY", None) or "").strip())
    from iic_booking.research_copilot.services.inference_concurrency import snapshot as concurrency_snapshot

    payload["concurrency"] = concurrency_snapshot().as_public_dict()
    return Response(payload)
