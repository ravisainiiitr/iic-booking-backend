"""AllowAny public Research Copilot endpoints (anonymous FAQ / slots / rough estimates)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from iic_booking.research_copilot.services import conversation as conv_svc
from iic_booking.research_copilot.throttles import ResearchCopilotAnonThrottle


@api_view(["GET"])
@permission_classes([AllowAny])
@throttle_classes([ResearchCopilotAnonThrottle])
def public_bootstrap(request):
    """Anonymous Copilot config. Soft-disabled returns enabled=false."""
    if not conv_svc.feature_enabled(user=None):
        payload = conv_svc.public_bootstrap_payload()
        payload["enabled"] = False
        return Response(payload)
    return Response(conv_svc.public_bootstrap_payload())


@api_view(["POST"])
@permission_classes([AllowAny])
@throttle_classes([ResearchCopilotAnonThrottle])
def public_ask(request):
    """One-shot anonymous ask (docs / public slots / rough estimates)."""
    if not conv_svc.feature_enabled(user=None):
        return Response(
            {
                "error": {
                    "code": "research_copilot_disabled",
                    "message": "IIC Research Copilot is not enabled on this environment.",
                }
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    # If the caller is authenticated, prefer the full conversation API.
    if request.user and request.user.is_authenticated:
        return Response(
            {
                "error": {
                    "code": "use_authenticated_api",
                    "message": "Signed-in users should use /conversations/ for full Copilot features.",
                }
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    text = ""
    if isinstance(request.data, dict):
        text = str(request.data.get("content") or request.data.get("message") or request.data.get("text") or "")
    result = conv_svc.public_ask(text=text)
    if not result.get("ok"):
        return Response(
            {"error": {"code": result.get("error") or "ask_failed", "message": result.get("message") or "Ask failed"}},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(result)
