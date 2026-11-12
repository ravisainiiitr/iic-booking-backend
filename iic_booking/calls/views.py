"""REST endpoints for in-app calls. Signalling itself runs over the WebSocket ``ws/calls/``."""

from __future__ import annotations

from datetime import datetime

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from . import conf, service, silent
from .models import CallDevice, CallSession, CallSilentHours, DevicePlatform
from .recipients import CallNotAllowed, calls_open_to, options


def _refusal(exc: CallNotAllowed) -> Response:
    return Response({"error": exc.message, "code": exc.code}, status=exc.status)


def _done(session, user):
    if session is None:
        return Response({"error": "Call not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    return Response(service.serialize(session, user))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def call_config(request):
    """Whether calling is open to this user, and the ICE servers (public STUN; TURN with short-lived credentials
    only when configured) for the WebRTC connection."""
    open_ = calls_open_to(request.user)
    return Response({
        "enabled": open_,
        "ice_servers": conf.ice_servers(request.user) if open_ else [],
        "turn": conf.turn_enabled(),
        "ring_seconds": conf.ring_seconds(),
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def call_options(request):
    """?context=booking|equipment|invoice&id=<id>: whom the user may call about it, with availability and fallback."""
    if not calls_open_to(request.user):
        return Response({"enabled": False, "roles": []})
    try:
        data = options(request.user, str(request.query_params.get("context") or ""), int(request.query_params.get("id") or 0))
    except (TypeError, ValueError):
        return Response({"error": "Unknown item.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    except CallNotAllowed as exc:
        return _refusal(exc)
    return Response({"enabled": True, **data})


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def calls(request):
    """GET: the user's call history (own calls only). POST {context, id, role, callee_id?, reason?, platform?}: call."""
    if request.method == "POST":
        try:
            session = service.start_call(
                request.user, context_type=str(request.data.get("context") or ""),
                context_id=int(request.data.get("id") or 0), role=str(request.data.get("role") or ""),
                callee_id=request.data.get("callee_id"), reason=str(request.data.get("reason") or ""),
                platform=str(request.data.get("platform") or "web"),
            )
        except (TypeError, ValueError):
            return Response({"error": "Unknown item.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
        except CallNotAllowed as exc:
            return _refusal(exc)
        return Response(service.serialize(session, request.user), status=status.HTTP_201_CREATED)
    mine = CallSession.objects.filter(Q(caller=request.user) | Q(callee=request.user))
    service.expire_stale(mine)
    rows = mine.select_related("caller__department", "callee__department")[:100]
    return Response({"results": [service.serialize(s, request.user, with_context=False) for s in rows]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def call_detail(request, call_id):
    session = CallSession.objects.select_related("caller", "callee").filter(
        Q(caller=request.user) | Q(callee=request.user), pk=call_id).first()
    if session is not None:
        service.expire_stale(CallSession.objects.filter(pk=session.pk))
        session.refresh_from_db()
    return _done(session, request.user)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def call_action(request, call_id, action):
    """accept | decline | cancel | end | fail (body: code) | timeout (caller's ring timer ran out)."""
    user = request.user
    try:
        if action == "accept":
            session = service.accept(user, call_id, platform=str(request.data.get("platform") or "web"))
        elif action == "decline":
            session = service.decline(user, call_id)
        elif action == "cancel":
            session = service.cancel(user, call_id)
        elif action == "timeout":
            session = service.cancel(user, call_id, timeout=True)
        elif action == "end":
            session = service.end(user, call_id)
        elif action == "fail":
            session = service.fail(user, call_id, str(request.data.get("code") or "failed"))
        else:
            return Response({"error": "Unknown action.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    except CallNotAllowed as exc:
        return _refusal(exc)
    except ValueError:
        session = None
    return _done(session, user)


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def call_decline_from_device(request, call_id):
    """The native incoming-call screen declines with the push's action token (the app may not be signed in yet)."""
    try:
        session = service.decline_with_token(call_id, str(request.data.get("token") or ""))
    except CallNotAllowed as exc:
        return _refusal(exc)
    return Response({"status": session.status if session else "unknown"})


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def call_status_from_device(request, call_id):
    """The native incoming-call screen (iPhone) asks whether the call is still ringing, with the push's action token."""
    try:
        state = service.status_with_token(call_id, str(request.data.get("token") or ""))
    except CallNotAllowed as exc:
        return _refusal(exc)
    return Response({"status": state})


def _silent_payload(equipment) -> dict:
    rule = CallSilentHours.objects.filter(equipment_id=equipment.pk).first()
    values = silent.DEFAULTS if rule is None else {
        "enabled": rule.enabled, "office_start": rule.office_start.strftime("%H:%M"), "office_end": rule.office_end.strftime("%H:%M"),
        "silent_weekends": rule.silent_weekends, "silent_holidays": rule.silent_holidays,
    }
    in_force = silent.rule_for(equipment)  # a mode without its own rule follows its base equipment
    return {**values, "active_now": silent.is_silent(in_force),
            "follows_base_equipment": rule is None and in_force is not None}


@api_view(["GET", "PUT"])
@permission_classes([IsAuthenticated])
def call_silent_hours(request, equipment_id):
    """An instrument's silent hours, for its Officer In Charge (and the admins who manage it)."""
    from iic_booking.equipment.flash_message_service import can_manage_equipment_flash
    from iic_booking.equipment.models import Equipment

    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if equipment is None:
        return Response({"error": "Equipment not found.", "code": "not_found"}, status=status.HTTP_404_NOT_FOUND)
    if not can_manage_equipment_flash(request.user, equipment):
        return Response({"error": "Only the Officer In Charge can change silent hours.", "code": "forbidden"}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        return Response(_silent_payload(equipment))
    try:
        start = datetime.strptime(str(request.data.get("office_start") or ""), "%H:%M").time()
        end = datetime.strptime(str(request.data.get("office_end") or ""), "%H:%M").time()
    except ValueError:
        return Response({"error": "Enter office hours as a start and end time.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    if start >= end:
        return Response({"error": "Office hours must end after they start.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    flags = {key: request.data.get(key) for key in ("enabled", "silent_weekends", "silent_holidays")}
    if not all(isinstance(value, bool) for value in flags.values()):
        return Response({"error": "Choose on or off for each option.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    CallSilentHours.objects.update_or_create(equipment=equipment, defaults={
        **flags, "office_start": start, "office_end": end, "updated_by": request.user})
    return Response(_silent_payload(equipment))


@api_view(["POST", "DELETE"])
@permission_classes([IsAuthenticated])
def call_devices(request):
    """POST {platform: android|ios, device_id, token, app_version?}: register or refresh this install's push token.
    DELETE {device_id, platform}: revoke it (sign-out). Tokens are never returned."""
    platform = str(request.data.get("platform") or "")
    device_id = str(request.data.get("device_id") or "")[:100]
    if platform not in DevicePlatform.values or not device_id:
        return Response({"error": "platform and device_id are required.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    if request.method == "DELETE":
        CallDevice.objects.filter(platform=platform, device_id=device_id, user=request.user).update(revoked_at=timezone.now())
        return Response({"ok": True})
    token = str(request.data.get("token") or "").strip()
    if not token or len(token) > 4096:
        return Response({"error": "token is required.", "code": "invalid"}, status=status.HTTP_400_BAD_REQUEST)
    # One row per install: a reinstall or another account signing in on the same phone takes the row over.
    CallDevice.objects.update_or_create(platform=platform, device_id=device_id, defaults={
        "user": request.user, "token": token, "app_version": str(request.data.get("app_version") or "")[:40],
        "revoked_at": None})
    return Response({"ok": True})
