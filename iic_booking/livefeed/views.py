"""Live feed endpoints: the sync agent's (agent-authenticated, under /api/v1/sync/live/) and the viewer's
(/api/live/)."""

from __future__ import annotations

from types import SimpleNamespace

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.calls.conf import ice_servers
from iic_booking.sync.authentication import DepartmentSyncAgentAuthentication
from iic_booking.sync.permissions import IsDepartmentSyncAgent

from . import service
from .models import LiveFeedPeer, LiveFeedSession
from .service import LiveFeedError


def _error(exc: LiveFeedError, code=status.HTTP_409_CONFLICT):
    return Response({"error": str(exc)}, status=code)


def _session_json(s: LiveFeedSession) -> dict:
    return {"id": s.pk, "equipment": {"id": s.equipment_id, "name": s.equipment.name}, "booking_id": s.booking_id,
            "sources": s.sources, "audio": s.audio, "status": s.status, "started_at": s.started_at.isoformat(),
            "slot_end": s.slot_end.isoformat(), "end_reason": s.end_reason}


# ---- sync agent ---------------------------------------------------------------------------------------------------

@api_view(["POST"])
@authentication_classes([DepartmentSyncAgentAuthentication])
@permission_classes([IsDepartmentSyncAgent])
def agent_start(request):
    """Body: {equipment_id, sources: ["screen", "camera"], audio, equipment_pc}."""
    data = request.data if isinstance(request.data, dict) else {}
    try:
        s = service.start(request.sync_agent, data.get("equipment_id"), sources=data.get("sources"),
                          audio=bool(data.get("audio", True)), equipment_pc=data.get("equipment_pc") or "")
    except LiveFeedError as exc:
        return _error(exc)
    agent_identity = SimpleNamespace(pk=f"agent-{request.sync_agent.pk}")
    return Response({"session_id": s.pk, "viewer": service._viewer_name(s), "slot_end": s.slot_end.isoformat(),
                     "ice_servers": ice_servers(agent_identity)}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@authentication_classes([DepartmentSyncAgentAuthentication])
@permission_classes([IsDepartmentSyncAgent])
def agent_poll(request, session_id):
    try:
        s = service.agent_session(request.sync_agent, session_id)
    except LiveFeedError as exc:
        return _error(exc, status.HTTP_404_NOT_FOUND)
    return Response(service.agent_poll(s))


@api_view(["POST"])
@authentication_classes([DepartmentSyncAgentAuthentication])
@permission_classes([IsDepartmentSyncAgent])
def agent_answer(request, session_id):
    """Body: {peer_id, sdp} or {peer_id, error}."""
    data = request.data if isinstance(request.data, dict) else {}
    try:
        s = service.agent_session(request.sync_agent, session_id)
        service.answer(s, data.get("peer_id"), sdp=str(data.get("sdp") or ""), error=str(data.get("error") or ""))
    except LiveFeedError as exc:
        return _error(exc)
    return Response({"ok": True})


@api_view(["POST"])
@authentication_classes([DepartmentSyncAgentAuthentication])
@permission_classes([IsDepartmentSyncAgent])
def agent_stop(request, session_id):
    try:
        s = service.agent_session(request.sync_agent, session_id)
    except LiveFeedError as exc:
        return _error(exc, status.HTTP_404_NOT_FOUND)
    if s.status == LiveFeedSession.ACTIVE:
        service.end(s, str((request.data or {}).get("reason") or "The Lab Operator stopped the live feed.")[:200])
    return Response({"ok": True})


# ---- viewer -------------------------------------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_sessions(request):
    """Live feeds the user may watch now (their booking's slot is in progress and the operator started it).
    ?booking=<id> narrows to one booking."""
    qs = service.sessions_for(request.user)
    if request.query_params.get("booking"):
        qs = qs.filter(booking_id=request.query_params["booking"])
    return Response({"results": [_session_json(s) for s in qs], "ice_servers": ice_servers(request.user)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_peer(request, session_id):
    """Body: {kind: "screen" | "camera", sdp}. Send the offer after ICE gathering completes (no trickle)."""
    data = request.data if isinstance(request.data, dict) else {}
    try:
        s = service.viewer_session(request.user, session_id)
        peer = service.offer(request.user, s, str(data.get("kind") or ""), str(data.get("sdp") or ""))
    except LiveFeedError as exc:
        return _error(exc, status.HTTP_404_NOT_FOUND if "not available" in str(exc) else status.HTTP_400_BAD_REQUEST)
    return Response({"peer_id": peer.pk, "status": peer.status}, status=status.HTTP_201_CREATED)


@api_view(["GET", "DELETE"])
@permission_classes([IsAuthenticated])
def peer_detail(request, peer_id):
    try:
        peer = service.viewer_peer(request.user, peer_id)
    except LiveFeedError as exc:
        return _error(exc, status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        service.close(peer)
        return Response(status=status.HTTP_204_NO_CONTENT)
    return Response({"peer_id": peer.pk, "status": peer.status, "error": peer.error,
                     "sdp": peer.answer_sdp if peer.status == LiveFeedPeer.ANSWERED else None,
                     "session_status": peer.session.status, "end_reason": peer.session.end_reason})
