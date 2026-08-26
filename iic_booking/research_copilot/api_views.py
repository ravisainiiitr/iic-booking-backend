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
from iic_booking.research_copilot.throttles import (
    ResearchCopilotMutationThrottle,
    ResearchCopilotToolThrottle,
    ResearchCopilotUserThrottle,
)


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


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotMutationThrottle])
def confirm_mutation(request):
    """
    Explicit confirmation endpoint for Phase B booking mutations.

    Body:
      proposal_id, confirmation_token, action?, idempotency_key?
    Flags must be ON for execute; otherwise returns disabled error.
    """
    gated = _feature_gate(user=request.user)
    if gated:
        return gated

    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut
    from iic_booking.research_copilot.services.v2.mutations import proposals as prop_store
    from iic_booking.research_copilot.services.v2.orchestrator import _exec_to_response

    proposal_id = (request.data.get("proposal_id") or "").strip()
    confirmation_token = (request.data.get("confirmation_token") or "").strip()
    action = (request.data.get("action") or "").strip().upper()
    idempotency_key = (request.data.get("idempotency_key") or "").strip()

    if not proposal_id or not confirmation_token:
        return Response(
            {"ok": False, "error": "CONFIRMATION_REQUIRED", "message": "proposal_id and confirmation_token are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    prop = prop_store.get_proposal(proposal_id)
    if not prop:
        return Response(
            {"ok": False, "error": "PROPOSAL_NOT_FOUND", "message": "Proposal not found or expired."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if int(prop.get("user_id") or 0) != int(request.user.pk):
        return Response(
            {"ok": False, "error": "PROPOSAL_FORBIDDEN", "message": "You cannot confirm this proposal."},
            status=status.HTTP_403_FORBIDDEN,
        )

    pending = action or prop.get("action") or "CREATE_BOOKING"
    if pending == "CANCEL_BOOKING":
        result = booking_mut.execute_booking_cancel(
            user=request.user,
            proposal_id=proposal_id,
            confirmation_token=confirmation_token,
            idempotency_key=idempotency_key,
        )
    elif pending == "RESCHEDULE_BOOKING":
        result = booking_mut.execute_booking_reschedule(
            user=request.user,
            proposal_id=proposal_id,
            confirmation_token=confirmation_token,
            idempotency_key=idempotency_key,
        )
    else:
        result = booking_mut.execute_booking_create(
            user=request.user,
            proposal_id=proposal_id,
            confirmation_token=confirmation_token,
            idempotency_key=idempotency_key,
        )

    envelope = _exec_to_response(result)
    http = status.HTTP_200_OK if result.get("ok") else status.HTTP_400_BAD_REQUEST
    # Disabled flags are not a client error — 403 communicates enablement gate
    if str(result.get("error") or "").endswith("_DISABLED"):
        http = status.HTTP_403_FORBIDDEN
    return Response({**result, "response": envelope}, status=http)


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
