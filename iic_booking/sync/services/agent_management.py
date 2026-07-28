"""Agent Management Dashboard JSON API (Milestone 11)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response

from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import (
    AgentCommand,
    AgentCommandPriority,
    AgentCommandStatus,
    AgentCommandType,
    AgentHeartbeat,
    AgentUploadSession,
    AgentUploadSessionStatus,
    DepartmentSyncAgent,
    ResultProcessingQueue,
    ResultProcessingStatus,
)
from iic_booking.sync.services.dataplane import CommandService


class AdminCommandCreateSerializer(serializers.Serializer):
    command_type = serializers.CharField(max_length=64)
    payload = serializers.JSONField(required=False, default=dict)
    priority = serializers.ChoiceField(
        choices=AgentCommandPriority.choices,
        required=False,
        default=AgentCommandPriority.NORMAL,
    )
    equipment_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)


def _is_online(last_heartbeat_at) -> bool:
    if last_heartbeat_at is None:
        return False
    cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())
    return last_heartbeat_at >= cutoff


def _serialize_agent(agent: DepartmentSyncAgent, latest: AgentHeartbeat | None) -> dict[str, Any]:
    details = (latest.details if latest else {}) or {}
    plugins = details.get("plugin_inventory") or details.get("plugins") or []
    current_upload = details.get("current_upload") or {}
    current_processing = details.get("current_processing") or {}

    eq = agent.equipment
    building = ""
    if eq is not None:
        building = (eq.location or eq.name or eq.code or "").strip()

    return {
        "id": str(agent.id),
        "agent_uuid": str(agent.agent_uuid),
        "agent_name": agent.agent_name,
        "online": _is_online(agent.last_heartbeat_at),
        "status": agent.status,
        "department": agent.department.name if agent.department_id else "",
        "department_code": getattr(agent.department, "code", "") or "",
        "building": building,
        "equipment_id": eq.equipment_id if eq is not None else None,
        "equipment_code": eq.code if eq is not None else "",
        "equipment_name": eq.name if eq is not None else "",
        "laboratory": eq.name if eq is not None else "",
        "computer": agent.machine_name or "",
        "hostname": (latest.hostname if latest else "") or agent.machine_name or "",
        "agent_version": agent.version or (latest.service_version if latest else "") or "",
        "plugin_versions": plugins,
        "windows_build": (latest.windows_build if latest else "") or "",
        "operating_system": agent.operating_system or "",
        "cpu_percent": latest.cpu_percent if latest else None,
        "memory_percent": latest.memory_percent if latest else None,
        "disk_percent": latest.disk_percent if latest else None,
        "storage_free_bytes": details.get("storage_free_bytes"),
        "storage_total_bytes": details.get("storage_total_bytes"),
        "queue_length": latest.queue_size if latest else None,
        "active_workers": latest.active_workers if latest else None,
        "current_upload": current_upload,
        "current_processing": current_processing,
        "last_heartbeat_at": agent.last_heartbeat_at.isoformat() if agent.last_heartbeat_at else None,
        "last_seen_at": agent.last_seen_at.isoformat() if agent.last_seen_at else None,
        "sqlite_schema_version": (latest.sqlite_schema_version if latest else "") or "",
        "capabilities": details.get("capabilities") or {},
    }


def _latest_heartbeat_map(agent_ids: list) -> dict:
    if not agent_ids:
        return {}
    rows = (
        AgentHeartbeat.objects.filter(sync_agent_id__in=agent_ids)
        .order_by("sync_agent_id", "-reported_at")
        .distinct("sync_agent_id")
        if False  # SQLite/MySQL may lack DISTINCT ON; use Python fallback
        else AgentHeartbeat.objects.filter(sync_agent_id__in=agent_ids).order_by("-reported_at")
    )
    latest: dict = {}
    for row in rows:
        if row.sync_agent_id not in latest:
            latest[row.sync_agent_id] = row
    return latest


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def admin_agents_list(request):
    """GET /api/v1/sync/admin/agents/ — Agent Management Dashboard rows."""
    qs = DepartmentSyncAgent.objects.select_related("department", "equipment").order_by(
        "department__name",
        "machine_name",
        "agent_name",
    )
    department = request.query_params.get("department")
    if department:
        qs = qs.filter(
            Q(department__code__iexact=department) | Q(department__name__icontains=department)
        )
    online_only = request.query_params.get("online")
    agents = list(qs[:500])
    latest = _latest_heartbeat_map([a.id for a in agents])
    results = [_serialize_agent(a, latest.get(a.id)) for a in agents]
    if online_only is not None:
        want_online = str(online_only).lower() in {"1", "true", "yes"}
        results = [r for r in results if bool(r["online"]) is want_online]

    online_count = sum(1 for r in results if r["online"])
    return Response(
        {
            "count": len(results),
            "online_count": online_count,
            "offline_count": len(results) - online_count,
            "heartbeat_timeout_seconds": heartbeat_timeout_seconds(),
            "server_time": timezone.now().isoformat(),
            "results": results,
        }
    )


@api_view(["GET"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def admin_agent_detail(request, agent_id):
    """GET /api/v1/sync/admin/agents/{id}/ — single agent dashboard + recent activity."""
    try:
        agent = DepartmentSyncAgent.objects.select_related("department", "equipment").get(pk=agent_id)
    except (DepartmentSyncAgent.DoesNotExist, ValueError):
        return Response({"detail": _("Agent not found.")}, status=status.HTTP_404_NOT_FOUND)

    latest = AgentHeartbeat.objects.filter(sync_agent=agent).order_by("-reported_at").first()
    payload = _serialize_agent(agent, latest)

    recent_heartbeats = [
        {
            "reported_at": h.reported_at.isoformat(),
            "cpu_percent": h.cpu_percent,
            "memory_percent": h.memory_percent,
            "disk_percent": h.disk_percent,
            "queue_size": h.queue_size,
            "active_workers": h.active_workers,
            "status_message": h.status_message,
        }
        for h in AgentHeartbeat.objects.filter(sync_agent=agent).order_by("-reported_at")[:20]
    ]

    pending_commands = list(
        AgentCommand.objects.filter(
            sync_agent=agent,
            status__in=[
                AgentCommandStatus.PENDING,
                AgentCommandStatus.ACKNOWLEDGED,
                AgentCommandStatus.RUNNING,
            ],
        )
        .order_by("-created_at")[:20]
        .values("id", "command_type", "status", "priority", "created_at", "payload")
    )
    for cmd in pending_commands:
        cmd["id"] = str(cmd["id"])
        cmd["created_at"] = cmd["created_at"].isoformat() if cmd["created_at"] else None

    active_uploads = list(
        AgentUploadSession.objects.filter(sync_agent=agent)
        .exclude(
            status__in=[
                AgentUploadSessionStatus.COMPLETED,
                AgentUploadSessionStatus.CANCELLED,
                AgentUploadSessionStatus.FAILED,
                AgentUploadSessionStatus.REJECTED,
                AgentUploadSessionStatus.EXPIRED,
            ]
        )
        .order_by("-updated_at")[:10]
        .values("id", "file_name", "status", "bytes_received", "expected_size")
    )
    for u in active_uploads:
        u["id"] = str(u["id"])

    active_processing = list(
        ResultProcessingQueue.objects.filter(sync_agent=agent)
        .exclude(
            status__in=[
                ResultProcessingStatus.COMPLETED,
                ResultProcessingStatus.FAILED,
                ResultProcessingStatus.CANCELLED,
            ]
        )
        .order_by("-updated_at")[:10]
        .values("id", "status", "parser_used", "agent_upload_id")
    )
    for p in active_processing:
        p["id"] = str(p["id"])
        p["agent_upload_id"] = str(p["agent_upload_id"])

    payload["recent_heartbeats"] = recent_heartbeats
    payload["pending_commands"] = pending_commands
    payload["active_uploads"] = active_uploads
    payload["active_processing"] = active_processing
    return Response(payload)


@api_view(["POST"])
@authentication_classes([SessionAuthentication])
@permission_classes([IsAdminUser])
def admin_agent_create_command(request, agent_id):
    """POST /api/v1/sync/admin/agents/{id}/commands/ — enqueue remote admin command."""
    try:
        agent = DepartmentSyncAgent.objects.get(pk=agent_id)
    except (DepartmentSyncAgent.DoesNotExist, ValueError):
        return Response({"detail": _("Agent not found.")}, status=status.HTTP_404_NOT_FOUND)

    serializer = AdminCommandCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    data = serializer.validated_data
    command_type = data["command_type"].strip().upper()

    allowed = {
        AgentCommandType.RESTART_AGENT,
        AgentCommandType.RESTART_PLUGIN,
        AgentCommandType.REFRESH_CONFIGURATION,
        AgentCommandType.RUN_DIAGNOSTICS,
        AgentCommandType.COLLECT_LOGS,
        AgentCommandType.BOOTSTRAP_REQUIRED,
        AgentCommandType.RESCAN_FOLDER,
        AgentCommandType.SYNCHRONIZE_BOOKINGS,
    }
    if command_type not in allowed:
        return Response(
            {"detail": _("Unsupported command_type for remote administration.")},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from iic_booking.sync.services.security import SecurityService

    if not SecurityService().authorize_remote_command(
        agent,
        command_type=command_type,
        user_name=getattr(request.user, "get_username", lambda: "")(),
        ip_address=request.META.get("REMOTE_ADDR"),
    ):
        return Response({"detail": _("Command not authorized for this agent.")}, status=status.HTTP_403_FORBIDDEN)

    equipment = None
    equipment_id = data.get("equipment_id")
    if equipment_id:
        from iic_booking.equipment.models import Equipment

        equipment = Equipment.objects.filter(pk=equipment_id).first()

    cmd = AgentCommand.objects.create(
        sync_agent=agent,
        equipment=equipment,
        command_type=command_type,
        priority=data.get("priority") or AgentCommandPriority.NORMAL,
        status=AgentCommandStatus.PENDING,
        payload=data.get("payload") or {},
        correlation_id=uuid.uuid4(),
        created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
    )
    return Response(CommandService()._serialize(cmd), status=status.HTTP_201_CREATED)
