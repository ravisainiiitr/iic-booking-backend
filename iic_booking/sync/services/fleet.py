"""Main Administrator fleet view: every department's Sync Agent at a glance, drill-down, and audited remote actions.

Endpoints (``/api/v1/sync/admin/fleet/...``) are Main Administrator only (``user_type=admin`` or superuser) —
narrower than the rest of the Department Sync console, which also admits Django staff.
"""

from __future__ import annotations

import base64
import binascii
import logging
import uuid
from datetime import timedelta
from typing import Any

from django.http import HttpResponse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from rest_framework import serializers, status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import BasePermission, IsAuthenticated
from rest_framework.response import Response

from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.authentication import SyncAgentUser
from iic_booking.sync.models import (
    AgentCommand,
    AgentCommandPriority,
    AgentCommandStatus,
    AgentCommandType,
    AgentDiagnosticState,
    AgentHealthState,
    AgentLifecycleStatus,
    AgentSupportBundle,
    AlertEvent,
    DepartmentSyncAgent,
)
from iic_booking.sync.portal_auth import PORTAL_ADMIN_AUTH
from iic_booking.sync.services import headless

logger = logging.getLogger(__name__)


class IsMainAdministrator(BasePermission):
    """Main Administrator (user_type=admin) or superuser. Staff alone is not enough for remote actions."""

    def has_permission(self, request, view):
        user = request.user
        if not user or not getattr(user, "is_authenticated", False) or isinstance(user, SyncAgentUser):
            return False
        if getattr(user, "is_superuser", False):
            return True
        from iic_booking.users.models.user_type import UserType

        return str(getattr(user, "user_type", "") or "").lower() == UserType.ADMIN


FLEET_PERM = [IsAuthenticated, IsMainAdministrator]

# Dashboard action -> agent command. Only these can be sent from the fleet view.
ACTIONS: dict[str, dict[str, Any]] = {
    "restart_service": {
        "command": AgentCommandType.RESTART_AGENT,
        "label": "Restart service",
        "priority": AgentCommandPriority.HIGH,
        "cooldown_minutes": 10,
    },
    "resync_bookings": {"command": AgentCommandType.SYNCHRONIZE_BOOKINGS, "label": "Re-sync bookings"},
    "retry_failed_uploads": {"command": AgentCommandType.RETRY_FAILED_UPLOADS, "label": "Retry failed uploads"},
    "support_bundle": {"command": AgentCommandType.COLLECT_SUPPORT_BUNDLE, "label": "Collect support bundle"},
    "refresh_equipment_pcs": {
        "command": AgentCommandType.REFRESH_EQUIPMENT_PCS,
        "label": "Refresh Equipment PCs",
    },
    "run_diagnostics": {"command": AgentCommandType.RUN_DIAGNOSTICS, "label": "Run diagnostics"},
}

# Disabled agents stay visible (with a reason); revoked/retired/deleted ones are gone from the fleet.
HIDDEN_STATUSES = [AgentLifecycleStatus.REVOKED, AgentLifecycleStatus.RETIRED, AgentLifecycleStatus.DELETED]
_OPEN_COMMAND = [AgentCommandStatus.PENDING, AgentCommandStatus.ACKNOWLEDGED, AgentCommandStatus.RUNNING]
_HEALTH_ORDER = {
    AgentHealthState.OFFLINE: 0,
    AgentHealthState.CRITICAL: 1,
    AgentHealthState.WARNING: 2,
    AgentHealthState.UNKNOWN: 3,
    AgentHealthState.HEALTHY: 4,
}


def _iso(value):
    return value.isoformat() if value is not None else None


def _section(diag: dict, name: str) -> dict:
    value = (diag or {}).get(name)
    return value if isinstance(value, dict) else {}


def _supported_commands(state: AgentDiagnosticState | None) -> list[str]:
    caps = (state.capabilities if state else None) or {}
    supported = caps.get("remote_commands")
    if isinstance(supported, list):
        return [str(x).upper() for x in supported]
    # Agents before 1.0.7 do not advertise; these are the commands they are known to run.
    return [
        AgentCommandType.RESTART_PLUGIN,
        AgentCommandType.REFRESH_CONFIGURATION,
        AgentCommandType.RUN_DIAGNOSTICS,
        AgentCommandType.COLLECT_LOGS,
        AgentCommandType.RESCAN_FOLDER,
        AgentCommandType.SYNCHRONIZE_BOOKINGS,
    ]


def _actions_for(state: AgentDiagnosticState | None) -> list[dict]:
    supported = set(_supported_commands(state))
    return [
        {"key": key, "label": spec["label"], "command_type": spec["command"], "supported": spec["command"] in supported}
        for key, spec in ACTIONS.items()
    ]


def _pc_counts(pcs: list, now) -> dict:
    limit = timedelta(minutes=headless._setting("DSA_EQPC_OFFLINE_MINUTES"))
    total = online = 0
    for pc in pcs or []:
        if not isinstance(pc, dict) or str(pc.get("registration_status") or "").lower() in ("unassigned", "retired"):
            continue
        total += 1
        seen = headless._dt(pc.get("last_seen"))
        if seen is not None and now - seen <= limit:
            online += 1
    return {"total": total, "online": online}


def serialize_summary(agent: DepartmentSyncAgent, state: AgentDiagnosticState | None, open_incidents: int, now) -> dict:
    diag = (state.diagnostics if state else None) or {}
    uploads = _section(diag, "uploads")
    storage = _section(diag, "storage")
    service = _section(diag, "service")
    reasons = (state.reasons if state else None) or []
    health = state.health if state else AgentHealthState.UNKNOWN
    # Offline is time-based: correct a stale stored state between periodic runs.
    timeout = heartbeat_timeout_seconds()
    if agent.last_heartbeat_at and (now - agent.last_heartbeat_at).total_seconds() > timeout:
        health = AgentHealthState.OFFLINE
    top = [r for r in reasons if r.get("severity") in ("critical", "warning")]
    if health == AgentHealthState.OFFLINE and not any(r.get("code") == "DSA_OFFLINE" for r in top):
        mins = int((now - agent.last_heartbeat_at).total_seconds() // 60)
        top.insert(0, {"code": "DSA_OFFLINE", "severity": "critical", "message": f"No heartbeat for {mins} min."})
    return {
        "id": str(agent.id),
        "agent_name": agent.agent_name,
        "machine_name": agent.machine_name or "",
        "department": agent.department.name if agent.department_id else "",
        "department_code": getattr(agent.department, "code", "") if agent.department_id else "",
        "status": agent.status,
        "health": health,
        "why": top[:3],
        "version": agent.version or service.get("version") or "",
        "last_heartbeat_at": _iso(agent.last_heartbeat_at),
        "uptime_seconds": service.get("uptime_seconds"),
        "uploads_pending": uploads.get("pending"),
        "uploads_failed": uploads.get("failed"),
        "oldest_pending_age_minutes": uploads.get("oldest_pending_age_minutes"),
        "disk_free_percent": storage.get("data_drive_free_percent"),
        "equipment_pcs": _pc_counts(state.equipment_pcs if state else [], now),
        "open_incidents": open_incidents,
        "clock_skew_seconds": state.clock_skew_seconds if state else None,
        "diagnostics_supported": bool(diag) and not diag.get("stale"),
    }


def _fleet_queryset():
    return DepartmentSyncAgent.objects.select_related("department").exclude(
        status__in=HIDDEN_STATUSES
    )


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(FLEET_PERM)
def fleet_list(request):
    """GET /api/v1/sync/admin/fleet/ — all departments' DSAs with health and why."""
    now = timezone.now()
    agents = list(_fleet_queryset()[:500])
    states = {s.sync_agent_id: s for s in AgentDiagnosticState.objects.filter(sync_agent__in=agents)}
    open_counts: dict = {}
    for row in (
        headless.open_incidents_all()
        .filter(sync_agent__in=agents)
        .values_list("sync_agent_id", flat=True)
    ):
        open_counts[row] = open_counts.get(row, 0) + 1
    rows = [serialize_summary(a, states.get(a.id), open_counts.get(a.id, 0), now) for a in agents]
    rows.sort(key=lambda r: (_HEALTH_ORDER.get(r["health"], 9), r["department"], r["agent_name"]))
    counts = {h: 0 for h in AgentHealthState.values}
    for r in rows:
        counts[r["health"]] = counts.get(r["health"], 0) + 1
    return Response(
        {
            "count": len(rows),
            "counts": counts,
            "heartbeat_timeout_seconds": heartbeat_timeout_seconds(),
            "server_time": now.isoformat(),
            "results": rows,
        }
    )


def _serialize_incident(a: AlertEvent) -> dict:
    d = a.details or {}
    return {
        "id": str(a.id),
        "code": a.rule_code,
        "title": a.title,
        "message": a.message,
        "severity": a.severity,
        "status": a.status,
        "subject": d.get("subject") or "",
        "opened_at": _iso(a.created_at),
        "resolved_at": _iso(a.resolved_at),
        "notified_at": d.get("notified_at"),
        "notify_count": d.get("notify_count") or 0,
        "acknowledged_by": a.acknowledged_by,
    }


def _serialize_command(c: AgentCommand) -> dict:
    result = c.result_payload or {}
    return {
        "id": str(c.id),
        "command_type": c.command_type,
        "status": c.status,
        "created_at": _iso(c.created_at),
        "completed_at": _iso(c.completed_at),
        "created_by": (getattr(c.created_by, "email", "") or "") if c.created_by_id else "",
        "reason": (c.payload or {}).get("reason", ""),
        "result": {k: v for k, v in result.items() if k != "bundle_zip_base64"},
        "last_error": c.last_error or "",
    }


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(FLEET_PERM)
def fleet_detail(request, agent_id):
    """GET /api/v1/sync/admin/fleet/{id}/ — drill-down: diagnostics, Equipment PCs, incidents, commands, bundles."""
    agent = _fleet_queryset().filter(pk=agent_id).first()
    if agent is None:
        return Response({"detail": _("Agent not found.")}, status=status.HTTP_404_NOT_FOUND)
    now = timezone.now()
    state = AgentDiagnosticState.objects.filter(sync_agent=agent).first()
    incidents = list(
        AlertEvent.objects.filter(sync_agent=agent, details__source=headless.SOURCE).order_by("-created_at")[:100]
    )
    open_count = sum(1 for i in incidents if i.status not in ("RESOLVED", "EXPIRED"))
    commands = list(
        AgentCommand.objects.filter(sync_agent=agent).select_related("created_by").order_by("-created_at")[:25]
    )
    bundles = list(
        AgentSupportBundle.objects.filter(sync_agent=agent)
        .defer("content")
        .order_by("-created_at")[:10]
    )
    latest_hb = agent.heartbeats.order_by("-reported_at").first()
    return Response(
        {
            "agent": serialize_summary(agent, state, open_count, now),
            "reasons": (state.reasons if state else []) or [],
            "diagnostics": (state.diagnostics if state else {}) or {},
            "equipment_pcs": (state.equipment_pcs if state else []) or [],
            "received_at": _iso(state.received_at) if state else None,
            "agent_sent_at": _iso(state.agent_sent_at) if state else None,
            "evaluated_at": _iso(state.evaluated_at) if state else None,
            "heartbeat": {
                "cpu_percent": latest_hb.cpu_percent,
                "memory_percent": latest_hb.memory_percent,
                "disk_percent": latest_hb.disk_percent,
                "queue_size": latest_hb.queue_size,
                "hostname": latest_hb.hostname,
                "windows_build": latest_hb.windows_build,
                "status_message": latest_hb.status_message,
                "reported_at": _iso(latest_hb.reported_at),
            }
            if latest_hb
            else None,
            "incidents": [_serialize_incident(i) for i in incidents],
            "commands": [_serialize_command(c) for c in commands],
            "bundles": [
                {
                    "id": str(b.id),
                    "file_name": b.file_name,
                    "size_bytes": b.size_bytes,
                    "truncated": b.truncated,
                    "created_at": _iso(b.created_at),
                }
                for b in bundles
            ],
            "actions": _actions_for(state),
        }
    )


class FleetActionSerializer(serializers.Serializer):
    action = serializers.ChoiceField(choices=sorted(ACTIONS))
    reason = serializers.CharField(max_length=300, required=False, allow_blank=True, default="")
    force = serializers.BooleanField(required=False, default=False)


@api_view(["POST"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(FLEET_PERM)
def fleet_action(request, agent_id):
    """POST /api/v1/sync/admin/fleet/{id}/actions/ — queue an audited troubleshooting command."""
    agent = _fleet_queryset().filter(pk=agent_id).first()
    if agent is None:
        return Response({"detail": _("Agent not found.")}, status=status.HTTP_404_NOT_FOUND)
    ser = FleetActionSerializer(data=request.data or {})
    ser.is_valid(raise_exception=True)
    key = ser.validated_data["action"]
    spec = ACTIONS[key]
    command_type = spec["command"]
    force = bool(ser.validated_data.get("force"))
    reason = (ser.validated_data.get("reason") or "").strip()
    user = request.user
    user_label = getattr(user, "email", "") or str(getattr(user, "pk", ""))
    ip = request.META.get("REMOTE_ADDR")

    state = AgentDiagnosticState.objects.filter(sync_agent=agent).first()
    if command_type not in _supported_commands(state):
        return Response(
            {"detail": _("This agent version cannot run that action. Update the agent first.")},
            status=status.HTTP_409_CONFLICT,
        )
    if AgentCommand.objects.filter(sync_agent=agent, command_type=command_type, status__in=_OPEN_COMMAND).exists():
        return Response(
            {"detail": _("The same action is already waiting for this agent.")}, status=status.HTTP_409_CONFLICT
        )
    cooldown = spec.get("cooldown_minutes")
    if cooldown and not force:
        recent = AgentCommand.objects.filter(
            sync_agent=agent,
            command_type=command_type,
            created_at__gte=timezone.now() - timedelta(minutes=cooldown),
        ).exists()
        if recent:
            return Response(
                {"detail": _("This action was sent less than %(m)s minutes ago.") % {"m": cooldown}},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

    from iic_booking.sync.services.security import SecurityService
    from iic_booking.sync.services.security_audit import SecurityAuditService

    if not SecurityService().authorize_remote_command(
        agent, command_type=command_type, user_name=user_label, ip_address=ip
    ):
        return Response({"detail": _("Command not authorized for this agent.")}, status=status.HTTP_403_FORBIDDEN)

    payload: dict[str, Any] = {"reason": reason, "requested_via": "fleet_dashboard", "action": key}
    if force:
        payload["force"] = True
    cmd = AgentCommand.objects.create(
        sync_agent=agent,
        command_type=command_type,
        priority=spec.get("priority") or AgentCommandPriority.NORMAL,
        status=AgentCommandStatus.PENDING,
        payload=payload,
        correlation_id=uuid.uuid4(),
        created_by=user,
    )
    SecurityAuditService().write(
        event_code="DSA-REMOTE-ACTION",
        message=f"Main Administrator queued {key} ({command_type})",
        sync_agent=agent,
        correlation_id=cmd.correlation_id,
        user_name=user_label,
        ip_address=ip,
        details={"command_id": str(cmd.id), "action": key, "reason": reason[:300], "force": force},
        durable=True,
    )
    return Response(_serialize_command(cmd), status=status.HTTP_201_CREATED)


@api_view(["POST"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(FLEET_PERM)
def fleet_incident_acknowledge(request, agent_id, incident_id):
    """POST /api/v1/sync/admin/fleet/{id}/incidents/{incident}/acknowledge/ — stop reminders for this incident."""
    alert = AlertEvent.objects.filter(
        pk=incident_id, sync_agent_id=agent_id, details__source=headless.SOURCE
    ).first()
    if alert is None:
        return Response({"detail": _("Incident not found.")}, status=status.HTTP_404_NOT_FOUND)
    if alert.status in ("RESOLVED", "EXPIRED"):
        return Response(_serialize_incident(alert))
    alert.status = "ACKNOWLEDGED"
    alert.acknowledged_by = (getattr(request.user, "email", "") or "")[:200]
    alert.acknowledged_at = timezone.now()
    # Acknowledged incidents are not repeated until the next DSA_ALERT_REPEAT_HOURS window.
    alert.details = {**(alert.details or {}), "notified_at": timezone.now().isoformat()}
    alert.save(update_fields=["status", "acknowledged_by", "acknowledged_at", "details", "updated_at"])
    return Response(_serialize_incident(alert))


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(FLEET_PERM)
def fleet_bundle_download(request, bundle_id):
    """GET /api/v1/sync/admin/fleet/bundles/{id}/download/ — the redacted support bundle zip."""
    bundle = AgentSupportBundle.objects.filter(pk=bundle_id).first()
    if bundle is None:
        return Response({"detail": _("Bundle not found.")}, status=status.HTTP_404_NOT_FOUND)
    response = HttpResponse(bytes(bundle.content), content_type="application/zip")
    safe_name = "".join(ch for ch in bundle.file_name if ch.isalnum() or ch in "-_.") or "dsa-support.zip"
    response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    response["Cache-Control"] = "no-store"
    return response


def store_support_bundle(command: AgentCommand, result: dict) -> dict:
    """Move the agent's base64 zip out of the command result into an AgentSupportBundle row."""
    raw = (result or {}).get("bundle_zip_base64")
    if not raw:
        return result or {}
    out = {k: v for k, v in result.items() if k != "bundle_zip_base64"}
    try:
        content = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError, TypeError):
        out["bundle_error"] = "Bundle was not valid base64."
        return out
    limit = int(headless._setting("DSA_SUPPORT_BUNDLE_MAX_BYTES"))
    if len(content) > limit or content[:2] != b"PK":
        out["bundle_error"] = "Bundle rejected (too large or not a zip)."
        return out
    name = str(result.get("bundle_file_name") or f"dsa-support-{command.id}.zip")[:200]
    bundle, _created = AgentSupportBundle.objects.update_or_create(
        command=command,
        defaults={
            "sync_agent": command.sync_agent,
            "file_name": name,
            "size_bytes": len(content),
            "truncated": bool(result.get("bundle_truncated")),
            "content": content,
        },
    )
    out["bundle_id"] = str(bundle.id)
    out["bundle_size_bytes"] = len(content)
    return out
