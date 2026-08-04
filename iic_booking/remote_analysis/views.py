"""Remote Analysis API views — agent control plane + admin management."""

from __future__ import annotations

import json

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.authentication import (
    RemoteAnalysisAgentAuthentication,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.permissions import (
    CanManageRemoteAnalysis,
    CanViewRemoteAnalysis,
    IsRemoteAnalysisAgent,
)
from iic_booking.remote_analysis.selectors import workstations as selectors
from iic_booking.remote_analysis.serializers import (
    AnalysisWorkstationSerializer,
    CreateCommandSerializer,
    InstalledSoftwareSerializer,
    MaintenanceWindowSerializer,
    RemoteCommandSerializer,
    WorkstationEventSerializer,
    WorkstationHeartbeatSerializer,
    WorkstationStateHistorySerializer,
)
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.services.heartbeat import HeartbeatService, mark_stale_workstations_offline
from iic_booking.remote_analysis.services.inventory import InventoryService
from iic_booking.remote_analysis.services.maintenance import MaintenanceService
from iic_booking.remote_analysis.services.registration import RegistrationService
from iic_booking.remote_analysis.services.workstation_admin import WorkstationAdminService

_AGENT_AUTH = [RemoteAnalysisAgentAuthentication]
_AGENT_PERM = [IsRemoteAnalysisAgent]
_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]
_VIEW = [IsAuthenticated, CanViewRemoteAnalysis]


def _department_scope(request):
    user = request.user
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type == "admin" or getattr(user, "is_superuser", False):
        return None
    return getattr(user, "department_id", None)


# ---------------------------------------------------------------------------
# Agent endpoints
# ---------------------------------------------------------------------------


@api_view(["POST"])
@authentication_classes([])
@permission_classes([AllowAny])
def register(request):
    """POST /api/v1/analysis/register/

    When RA_AGENT_ENROLLMENT_KEY is set, require matching X-Enrollment-Key
    (or body enrollmentKey) — production gate for open registration.
    """
    import hmac
    import os

    expected = (os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip()
    if expected:
        provided = (
            request.META.get("HTTP_X_ENROLLMENT_KEY")
            or request.headers.get("X-Enrollment-Key")
            or ""
        ).strip()
        if not provided:
            try:
                data = request.data if isinstance(request.data, dict) else {}
                provided = str(data.get("enrollmentKey") or data.get("enrollment_key") or "").strip()
            except Exception:
                provided = ""
        if not provided or not hmac.compare_digest(provided, expected):
            return Response(
                {"accepted": False, "message": "Invalid or missing enrollment key."},
                status=status.HTTP_403_FORBIDDEN,
            )

    try:
        result = RegistrationService().register(request.data if isinstance(request.data, dict) else {})
    except ValueError as exc:
        return Response({"accepted": False, "message": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(result, status=status.HTTP_201_CREATED if result.get("created") else status.HTTP_200_OK)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def heartbeat(request):
    """POST /api/v1/analysis/heartbeat/"""
    mark_stale_workstations_offline()
    workstation = request.user.workstation
    result = HeartbeatService().process(workstation, request.data if isinstance(request.data, dict) else {})
    return Response(result)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def inventory(request):
    """POST /api/v1/analysis/inventory/"""
    workstation = request.user.workstation
    result = InventoryService().synchronize(workstation, request.data if isinstance(request.data, dict) else {})
    return Response(result)


@api_view(["GET"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def commands_poll(request):
    """GET /api/v1/analysis/commands/"""
    workstation = request.user.workstation
    pending = CommandService().poll_pending(workstation)
    payload = [
        {
            "id": str(cmd.id),
            "type": cmd.command_type,
            "payloadJson": json.dumps(cmd.payload) if cmd.payload else None,
        }
        for cmd in pending
    ]
    return Response(payload)


@api_view(["POST"])
@authentication_classes(_AGENT_AUTH)
@permission_classes(_AGENT_PERM)
def command_complete(request, command_id):
    """POST /api/v1/analysis/commands/{id}/complete/"""
    workstation = request.user.workstation
    command = get_object_or_404(RemoteCommand, pk=command_id, workstation=workstation)
    success = bool(request.data.get("success", True))
    message = str(request.data.get("message") or "")
    CommandService().complete(command, success=success, message=message)
    return Response({"accepted": True, "status": command.status})


# ---------------------------------------------------------------------------
# Portal admin / dashboard endpoints
# ---------------------------------------------------------------------------


@api_view(["GET"])
@permission_classes(_VIEW)
def workstations_list(request):
    """GET /api/v1/analysis/workstations/"""
    mark_stale_workstations_offline()
    qs = selectors.workstations_queryset(department_id=_department_scope(request))
    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter.upper())
    return Response(AnalysisWorkstationSerializer(qs, many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def workstation_detail(request, workstation_id):
    """GET /api/v1/analysis/workstations/{id}/"""
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    dept = _department_scope(request)
    if dept is not None and ws.department_id not in (None, dept):
        return Response({"detail": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    data = AnalysisWorkstationSerializer(ws).data
    data["recent_heartbeats"] = WorkstationHeartbeatSerializer(
        selectors.recent_heartbeats(ws, limit=20), many=True
    ).data
    data["state_history"] = WorkstationStateHistorySerializer(ws.state_history.all()[:20], many=True).data
    data["software"] = InstalledSoftwareSerializer(
        selectors.installed_software(ws)[:100], many=True
    ).data
    return Response(data)


@api_view(["GET"])
@permission_classes(_VIEW)
def dashboard(request):
    """GET /api/v1/analysis/dashboard/"""
    mark_stale_workstations_offline()
    return Response(selectors.dashboard_metrics(department_id=_department_scope(request)))


@api_view(["POST"])
@permission_classes(_MANAGE)
def workstation_maintenance(request, workstation_id):
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    data = request.data or {}
    reason = str(data.get("reason") or "")
    try:
        WorkstationAdminService().set_maintenance(
            ws,
            actor=request.user,
            reason=reason,
            kind=str(data.get("kind") or ""),
            end=data.get("end") or data.get("expected_end"),
            description=str(data.get("description") or ""),
            assigned_engineer=str(data.get("assigned_engineer") or ""),
            amc_reference=str(data.get("amc_reference") or ""),
            ticket_number=str(data.get("ticket_number") or ""),
            maintenance_notes=str(data.get("maintenance_notes") or data.get("notes") or ""),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    ws.refresh_from_db()
    return Response(AnalysisWorkstationSerializer(ws).data)


@api_view(["GET", "POST"])
@permission_classes(_MANAGE)
def maintenance_windows_collection(request):
    from iic_booking.remote_analysis.scheduler_models import MaintenanceWindow

    if request.method == "GET":
        qs = MaintenanceWindow.objects.select_related("workstation").order_by("-start")[:200]
        active_only = str(request.query_params.get("active") or "").lower() in {"1", "true", "yes"}
        if active_only:
            qs = qs.filter(active=True)
        return Response(MaintenanceWindowSerializer(qs, many=True).data)

    data = request.data or {}
    ws = None
    ws_id = data.get("workstation") or data.get("workstation_id")
    if ws_id:
        ws = get_object_or_404(AnalysisWorkstation, pk=ws_id)
    try:
        window = MaintenanceService().schedule(
            workstation=ws,
            kind=str(data.get("kind") or "MAINTENANCE"),
            start=data.get("start"),
            end=data.get("end") or data.get("expected_end"),
            reason=str(data.get("reason") or ""),
            description=str(data.get("description") or ""),
            assigned_engineer=str(data.get("assigned_engineer") or ""),
            amc_reference=str(data.get("amc_reference") or ""),
            ticket_number=str(data.get("ticket_number") or ""),
            maintenance_notes=str(data.get("maintenance_notes") or data.get("notes") or ""),
            restore_status=str(data.get("restore_status") or "AVAILABLE"),
            actor=request.user,
            apply_immediately=bool(data.get("apply_immediately", True)),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(MaintenanceWindowSerializer(window).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes(_MANAGE)
def maintenance_window_end(request, window_id):
    from iic_booking.remote_analysis.scheduler_models import MaintenanceWindow

    window = get_object_or_404(MaintenanceWindow, pk=window_id)
    result = MaintenanceService().restore_window(window, actor=request.user)
    window.refresh_from_db()
    return Response({"window": MaintenanceWindowSerializer(window).data, "result": result})


@api_view(["GET"])
@permission_classes(_VIEW)
def fleet_dashboard(request):
    """GET /api/v1/analysis/fleet/ — Analysis PC operational fleet summary."""
    mark_stale_workstations_offline()
    payload = MaintenanceService().fleet_dashboard(department_id=_department_scope(request))
    return Response(payload)


@api_view(["GET"])
@permission_classes(_VIEW)
def fleet_inventory_view(request):
    """GET /api/v1/analysis/fleet/inventory/ — detailed per-PC health inventory."""
    from iic_booking.remote_analysis.services.fleet_inventory import fleet_inventory

    mark_stale_workstations_offline()
    status_filter = request.query_params.get("status")
    return Response(
        fleet_inventory(department_id=_department_scope(request), status=status_filter)
    )


@api_view(["GET", "POST"])
@permission_classes(_MANAGE)
def fleet_duplicates(request):
    from iic_booking.remote_analysis.services.workstation_identity import WorkstationIdentityService

    svc = WorkstationIdentityService()
    if request.method == "GET":
        return Response({"groups": svc.list_duplicates()})
    data = request.data or {}
    if data.get("auto"):
        return Response(svc.auto_merge_hostname_duplicates(actor=request.user, archive=True))
    survivor = data.get("survivor_id")
    dupes = data.get("duplicate_ids") or []
    if not survivor or not dupes:
        return Response({"detail": "survivor_id and duplicate_ids required"}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        svc.merge(
            survivor_id=survivor,
            duplicate_ids=dupes,
            actor=request.user,
            archive=bool(data.get("archive", True)),
            delete=bool(data.get("delete", False)),
        )
    )


@api_view(["GET"])
@permission_classes(_VIEW)
def equipment_config_audit_view(request):
    from iic_booking.remote_analysis.services.fleet_inventory import equipment_ra_config_audit

    return Response(equipment_ra_config_audit())


@api_view(["POST", "GET"])
@permission_classes(_MANAGE)
def run_commissioning(request):
    """GET/POST /api/v1/analysis/commissioning/run/ — automatic production readiness checks."""
    from iic_booking.remote_analysis.services.production_commissioning import run_production_commissioning

    mark_stale_workstations_offline()
    return Response(run_production_commissioning())


@api_view(["POST"])
@permission_classes(_MANAGE)
def workstation_enable(request, workstation_id):
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    WorkstationAdminService().enable(ws, actor=request.user)
    return Response(AnalysisWorkstationSerializer(ws).data)


@api_view(["POST"])
@permission_classes(_MANAGE)
def workstation_disable(request, workstation_id):
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    WorkstationAdminService().disable(ws, actor=request.user)
    return Response(AnalysisWorkstationSerializer(ws).data)


@api_view(["POST"])
@permission_classes(_MANAGE)
def workstation_create_command(request, workstation_id):
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    ser = CreateCommandSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    try:
        cmd = CommandService().create_command(
            ws,
            ser.validated_data["command_type"],
            payload=ser.validated_data.get("payload") or {},
            created_by=request.user,
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(RemoteCommandSerializer(cmd).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes(_VIEW)
def software_list(request):
    workstation_id = request.query_params.get("workstation")
    ws = None
    if workstation_id:
        ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    return Response(InstalledSoftwareSerializer(selectors.installed_software(ws)[:500], many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def commands_list(request):
    workstation_id = request.query_params.get("workstation")
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id) if workstation_id else None
    return Response(RemoteCommandSerializer(selectors.recent_commands(workstation=ws, limit=100), many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def events_list(request):
    workstation_id = request.query_params.get("workstation")
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id) if workstation_id else None
    return Response(WorkstationEventSerializer(selectors.recent_events(workstation=ws, limit=200), many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def heartbeats_list(request):
    workstation_id = request.query_params.get("workstation")
    if not workstation_id:
        return Response({"detail": "workstation query parameter required"}, status=status.HTTP_400_BAD_REQUEST)
    ws = get_object_or_404(AnalysisWorkstation, pk=workstation_id)
    return Response(
        WorkstationHeartbeatSerializer(selectors.recent_heartbeats(ws, limit=100), many=True).data
    )
