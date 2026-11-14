"""HTTP endpoints for Remote Analysis large data (agent token auth for agents; portal auth for people)."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.core.exceptions import ValidationError
from django.utils import timezone
from django.http import FileResponse
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.authentication import RemoteAnalysisAgentAuthentication
from iic_booking.remote_analysis.large_data import outbox, preload, speedtest
from iic_booking.remote_analysis.large_data.common import TransferError
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis, IsRemoteAnalysisAgent
from iic_booking.remote_analysis.transfer_models import InputPreload, SpeedTestStatus, TransferSpeedTest

logger = logging.getLogger(__name__)

_AGENT_AUTH = [RemoteAnalysisAgentAuthentication]
_AGENT = [IsRemoteAnalysisAgent]


def _error(exc: TransferError) -> Response:
    return Response(exc.as_dict(), status=exc.status)


def _storage_error(exc: Exception) -> Response:
    logger.warning("Large-data storage call failed: %s", exc.__class__.__name__)
    return Response({"code": "storage_error", "detail": "Cloud storage is not reachable; retry shortly."}, status=503)


def _agent_ws(request):
    return getattr(request.user, "workstation", None)


def _call(fn):
    from iic_booking.my_research.storage import ResearchStorageError

    try:
        return fn()
    except TransferError as exc:
        return _error(exc)
    except ResearchStorageError as exc:
        return _storage_error(exc)


# --- Phase 0 ------------------------------------------------------------------------------------------------------


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT)
def agent_speed_test_result(request, test_id):
    """POST /api/v1/analysis/agent/speed-test/{test_id}/result/ {upload_mbps, download_mbps, bytes, seconds, measured_at}"""
    test = get_object_or_404(TransferSpeedTest, pk=test_id, workstation=_agent_ws(request))
    if test.status != SpeedTestStatus.PENDING:
        return Response(speedtest.serialize(test))
    data = request.data if isinstance(request.data, dict) else {}
    ok = data.get("success", True) not in (False, "false", 0)
    return _call(lambda: Response(speedtest.serialize(speedtest.record_result(test, data, success=ok, message=str(data.get("detail") or "")))))


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated, CanManageRemoteAnalysis])
def workstation_speed_test(request, workstation_id):
    """GET: latest result. POST: queue SPEED_TEST on this Analysis PC."""
    from iic_booking.remote_analysis.large_data.common import CAP_SPEED_TEST, agent_supports

    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    if request.method == "GET":
        return Response(speedtest.summary(workstation=ws))
    if not agent_supports(ws, CAP_SPEED_TEST):
        return Response(
            {"code": "unsupported", "detail": "This Analysis PC needs an agent update before it can run a speed test."},
            status=409,
        )
    if TransferSpeedTest.objects.filter(
        workstation=ws, status=SpeedTestStatus.PENDING, created_at__gte=timezone.now() - timedelta(minutes=30)
    ).exists():
        return Response({"code": "already_queued", "detail": "A speed test is already waiting for this PC."}, status=409)
    try:
        test, cmd = speedtest.issue_for_workstation(ws, requested_by=request.user)
    except TransferError as exc:
        return _error(exc)
    except Exception as exc:  # noqa: BLE001
        return _storage_error(exc)
    return Response({"test_id": str(test.id), "command_id": str(cmd.id)}, status=status.HTTP_201_CREATED)


# --- Phase 1 ------------------------------------------------------------------------------------------------------


def _reservation_for_agent(request, reservation_id):
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    ws = _agent_ws(request)
    reservation = AnalysisReservation.objects.select_related("booking", "workstation").filter(pk=reservation_id).first()
    if reservation is None or ws is None:
        return None
    if reservation.workstation_id == ws.id:
        return reservation
    if InputPreload.objects.filter(reservation=reservation, workstation=ws).exists():
        return reservation
    return None


def _forbidden():
    return Response({"code": "forbidden", "detail": "This reservation is not on this Analysis PC."}, status=403)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT)
def agent_input_manifest(request, reservation_id):
    """GET /api/v1/analysis/agent/transfers/input/{reservation_id}/manifest/"""
    reservation = _reservation_for_agent(request, reservation_id)
    if reservation is None:
        return _forbidden()
    pre = InputPreload.objects.filter(reservation=reservation).first()
    if pre is not None and pre.state == "discarded" and pre.workstation_id == _agent_ws(request).id:
        return Response({"code": "discarded", "detail": "This pre-load was discarded; delete the holding folder."}, status=409)
    return _call(lambda: Response(preload.manifest(reservation, build_url=request.build_absolute_uri)))


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT)
def agent_input_progress(request, reservation_id):
    """POST .../input/{reservation_id}/progress/ {state, files_done, files_total, bytes_done, bytes_total, detail}"""
    reservation = _reservation_for_agent(request, reservation_id)
    if reservation is None:
        return _forbidden()
    pre = InputPreload.objects.filter(reservation=reservation).first()
    if pre is None or pre.workstation_id != _agent_ws(request).id:
        return Response({"code": "not_queued", "detail": "No pre-load is queued for this PC."}, status=409)
    data = request.data if isinstance(request.data, dict) else {}
    return _call(lambda: Response(_preload_ack(preload.record_progress(pre, data))))


def _preload_ack(pre: InputPreload) -> dict:
    return {"ok": True, "state": pre.state, "revision": pre.revision}


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT)
def agent_input_file_content(request, reservation_id, file_id):
    """GET .../input/{reservation_id}/files/{file_id}/content/ — only for legacy files kept on the portal."""
    reservation = _reservation_for_agent(request, reservation_id)
    if reservation is None:
        return _forbidden()
    try:
        opened = preload.open_content(reservation, file_id)
    except (ValueError, ValidationError):
        opened = None
    if opened is None:
        return Response({"code": "not_found", "detail": "Unknown file, or the file has a download URL."}, status=404)
    fileobj, size, name = opened
    response = FileResponse(fileobj, as_attachment=True, filename=name, content_type="application/octet-stream")
    response["Content-Length"] = str(size)
    return response


# --- Phase 2 ------------------------------------------------------------------------------------------------------


def _session_for_agent(request, session_id):
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    ws = _agent_ws(request)
    session = RemoteDesktopSession.objects.select_related("booking", "user", "workstation").filter(pk=session_id).first()
    if session is None or ws is None or session.workstation_id != ws.id:
        return None
    return session


def _output_view(handler):
    def view(request, session_id):
        session = _session_for_agent(request, session_id)
        if session is None:
            return Response({"code": "forbidden", "detail": "This session did not run on this Analysis PC."}, status=403)
        data = request.data if isinstance(request.data, dict) else {}
        return _call(lambda: Response(handler(session, data)))

    return view


def _progress_payload(session, data):
    box = outbox.progress(session, data)
    return {"ok": True, "state": box.state}


agent_output_init = api_view(["POST"])(authentication_classes(_AGENT_AUTH)(permission_classes(_AGENT)(_output_view(outbox.init))))
agent_output_parts = api_view(["POST"])(authentication_classes(_AGENT_AUTH)(permission_classes(_AGENT)(_output_view(outbox.parts))))
agent_output_state = api_view(["GET", "POST"])(
    authentication_classes(_AGENT_AUTH)(permission_classes(_AGENT)(_output_view(lambda session, _data: outbox.state(session))))
)
agent_output_complete = api_view(["POST"])(
    authentication_classes(_AGENT_AUTH)(permission_classes(_AGENT)(_output_view(outbox.complete)))
)
agent_output_progress = api_view(["POST"])(
    authentication_classes(_AGENT_AUTH)(permission_classes(_AGENT)(_output_view(_progress_payload)))
)


# --- Part 5 -------------------------------------------------------------------------------------------------------


def _booking_auth():
    from rest_framework.authentication import SessionAuthentication

    from iic_booking.remote_analysis.operations.commissioning import QueryParamTokenAuthentication
    from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity

    # Same as the other /bookings/{id}/analysis/ endpoints.
    return [TokenAuthenticationWithInactivity, SessionAuthentication, QueryParamTokenAuthentication]


@api_view(["POST"])
@authentication_classes(_booking_auth())
@permission_classes([IsAuthenticated])
def booking_keep_running(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/keep-running/ (also under /api/v1/equipment/bookings/...)."""
    from iic_booking.equipment.models import Booking
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import open_session
    from iic_booking.equipment.remote_analysis_integration.views import _can_launch
    from iic_booking.remote_analysis.large_data import keep_running

    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_launch(request.user, booking):
        return Response({"detail": "Permission denied.", "code": "forbidden"}, status=403)
    session = open_session(booking)
    if session is None:
        return Response({"code": "no_active_session", "detail": "Your analysis session is not running."}, status=409)
    try:
        return Response(keep_running.start(session, user=request.user))
    except TransferError as exc:
        return _error(exc)
