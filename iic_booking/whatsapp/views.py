"""WhatsApp endpoints: the signed-in user's opt-in, the Main Administrator's status, and Meta's webhook."""

import json
import logging

from django.http import HttpResponse
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import conf
from .client import signature_valid
from .service import OptInError, handle_webhook, opt_in_status, set_opt_in, status_summary

logger = logging.getLogger(__name__)


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def whatsapp_me(request):
    """GET the user's WhatsApp opt-in; PUT {"opted_in": true, "phone": "+91…"} to opt in or {"opted_in": false}."""
    if request.method == "GET":
        return Response(opt_in_status(request.user))
    try:
        return Response(set_opt_in(request.user, opted_in=bool(request.data.get("opted_in")),
                                   phone=str(request.data.get("phone") or "")))
    except OptInError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def whatsapp_status(request):
    """Main Administrator: whether WhatsApp is configured and on, opted-in users and message counts."""
    from iic_booking.department_modules.access import is_main_admin

    if not is_main_admin(request.user):
        return Response({"error": "Only the Main Administrator can see WhatsApp status."}, status=status.HTTP_403_FORBIDDEN)
    return Response(status_summary())


@api_view(["GET", "POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def whatsapp_webhook(request):
    """Meta's webhook. GET: subscription check (hub.verify_token). POST: signed delivery statuses and replies."""
    if request.method == "GET":
        token = request.query_params.get("hub.verify_token", "")
        if conf.verify_token() and token == conf.verify_token() and request.query_params.get("hub.mode") == "subscribe":
            return HttpResponse(request.query_params.get("hub.challenge", ""), content_type="text/plain")
        return HttpResponse(status=403)
    raw = request.body
    if not signature_valid(raw, request.headers.get("X-Hub-Signature-256", "")):
        return HttpResponse(status=403)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return HttpResponse(status=400)
    result = handle_webhook(payload)
    logger.info("whatsapp webhook: %s", result)
    return Response({"ok": True})
