"""Portal console JSON APIs for Main Administrator Department Sync hub."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.response import Response

from iic_booking.sync.admin.dashboard import build_operations_dashboard_context
from iic_booking.sync.models import (
    AgentAssignment,
    AgentCommand,
    AgentHeartbeat,
    BookingWorkspace,
    EquipmentSyncProfile,
    SyncLog,
)
from iic_booking.sync.portal_auth import PORTAL_ADMIN_AUTH, PORTAL_ADMIN_PERM


def _iso(value):
    return value.isoformat() if value is not None else None


def _serialize_log(row: SyncLog) -> dict:
    return {
        "id": row.id,
        "event_code": row.event_code,
        "severity": row.severity,
        "category": row.category,
        "message": row.message,
        "agent_id": str(row.sync_agent_id) if row.sync_agent_id else None,
        "agent_name": getattr(row.sync_agent, "agent_name", "") if row.sync_agent_id else "",
        "equipment_id": row.equipment_id,
        "equipment_name": getattr(row.equipment, "name", "") if row.equipment_id else "",
        "created_at": _iso(row.created_at),
    }


def _serialize_heartbeat(row: AgentHeartbeat) -> dict:
    return {
        "id": row.id,
        "agent_id": str(row.sync_agent_id),
        "agent_name": getattr(row.sync_agent, "agent_name", ""),
        "reported_at": _iso(row.reported_at),
        "cpu_percent": row.cpu_percent,
        "memory_percent": row.memory_percent,
        "disk_percent": row.disk_percent,
        "queue_size": row.queue_size,
        "active_workers": row.active_workers,
        "hostname": row.hostname or "",
        "service_version": getattr(row, "service_version", "") or "",
        "status_message": row.status_message or "",
    }


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_console_overview(request):
    """GET /api/v1/sync/admin/console/ — operations overview for Main Admin hub."""
    ctx = build_operations_dashboard_context(limit_recent=15)
    return Response(
        {
            "cards": ctx["cards"],
            "heartbeat_timeout_seconds": ctx["heartbeat_timeout_seconds"],
            "generated_at": _iso(ctx["generated_at"]),
            "validation_issues": [
                {
                    "code": getattr(issue, "code", ""),
                    "severity": getattr(issue, "severity", ""),
                    "message": getattr(issue, "message", str(issue)),
                    "object_repr": getattr(issue, "object_repr", ""),
                }
                for issue in (ctx.get("validation_issues") or [])
            ],
            "recent_activity": [_serialize_log(row) for row in ctx["recent_activity"]],
            "recent_errors": [_serialize_log(row) for row in ctx["recent_errors"]],
            "recent_heartbeats": [_serialize_heartbeat(row) for row in ctx["recent_heartbeats"]],
            "recent_registrations": [
                {
                    "id": str(a.id),
                    "agent_name": a.agent_name,
                    "department": a.department.name if a.department_id else "",
                    "equipment_name": a.equipment.name if a.equipment_id else "",
                    "status": a.status,
                    "registered_at": _iso(a.registered_at),
                }
                for a in ctx["recent_registrations"]
            ],
        }
    )


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_profiles_list(request):
    """GET /api/v1/sync/admin/profiles/ — Equipment Sync Profiles."""
    qs = (
        EquipmentSyncProfile.objects.select_related(
            "equipment",
            "equipment__internal_department",
            "primary_agent",
            "building",
        )
        .order_by("equipment__name")[:500]
    )
    results = []
    for p in qs:
        eq = p.equipment
        dept = getattr(eq, "internal_department", None) if eq else None
        results.append(
            {
                "id": str(p.id),
                "equipment_id": eq.equipment_id if eq else None,
                "equipment_code": eq.code if eq else "",
                "equipment_name": eq.name if eq else "",
                "department": dept.name if dept else "",
                "building": p.building.name if p.building_id else "",
                "hostname": p.hostname,
                "ip_address": p.ip_address,
                "watch_folder": p.watch_folder,
                "unc_path": p.unc_path,
                "sync_enabled": p.sync_enabled,
                "configuration_version": p.configuration_version,
                "schema_version": p.schema_version,
                "primary_agent_id": str(p.primary_agent_id) if p.primary_agent_id else None,
                "primary_agent_name": p.primary_agent.agent_name if p.primary_agent_id else "",
            }
        )
    return Response({"count": len(results), "results": results})


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_assignments_list(request):
    """GET /api/v1/sync/admin/assignments/ — Agent Assignments."""
    active_only = str(request.query_params.get("active") or "1").lower() in {"1", "true", "yes"}
    qs = AgentAssignment.objects.select_related(
        "sync_agent",
        "sync_agent__department",
        "sync_profile",
        "sync_profile__equipment",
    ).order_by("-assigned_at")
    if active_only:
        qs = qs.filter(is_active=True)
    qs = qs[:500]
    results = [
        {
            "id": str(a.id),
            "is_active": a.is_active,
            "assigned_at": _iso(a.assigned_at),
            "unassigned_at": _iso(a.unassigned_at),
            "notes": a.notes or "",
            "agent_id": str(a.sync_agent_id),
            "agent_name": a.sync_agent.agent_name,
            "department": a.sync_agent.department.name if a.sync_agent.department_id else "",
            "profile_id": str(a.sync_profile_id),
            "equipment_id": a.sync_profile.equipment_id if a.sync_profile_id else None,
            "equipment_name": (
                a.sync_profile.equipment.name
                if a.sync_profile_id and a.sync_profile.equipment_id
                else ""
            ),
            "equipment_code": (
                a.sync_profile.equipment.code
                if a.sync_profile_id and a.sync_profile.equipment_id
                else ""
            ),
        }
        for a in qs
    ]
    return Response({"count": len(results), "results": results})


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_heartbeats_list(request):
    """GET /api/v1/sync/admin/heartbeats/ — recent Agent Heartbeats."""
    agent_id = (request.query_params.get("agent_id") or "").strip()
    qs = AgentHeartbeat.objects.select_related("sync_agent").order_by("-reported_at")
    if agent_id:
        qs = qs.filter(sync_agent_id=agent_id)
    rows = list(qs[:200])
    return Response({"count": len(rows), "results": [_serialize_heartbeat(row) for row in rows]})


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_commands_list(request):
    """GET /api/v1/sync/admin/commands/ — Agent Commands queue."""
    agent_id = (request.query_params.get("agent_id") or "").strip()
    status_filter = (request.query_params.get("status") or "").strip().upper()
    qs = AgentCommand.objects.select_related("sync_agent", "equipment", "created_by").order_by(
        "-created_at"
    )
    if agent_id:
        qs = qs.filter(sync_agent_id=agent_id)
    if status_filter:
        qs = qs.filter(status=status_filter)
    rows = list(qs[:300])
    results = []
    for c in rows:
        results.append(
            {
                "id": str(c.id),
                "command_type": c.command_type,
                "status": c.status,
                "priority": c.priority,
                "agent_id": str(c.sync_agent_id),
                "agent_name": c.sync_agent.agent_name,
                "equipment_id": c.equipment_id,
                "equipment_name": c.equipment.name if c.equipment_id else "",
                "created_by": (
                    (getattr(c.created_by, "name", None) or getattr(c.created_by, "email", "") or "")
                    if c.created_by_id
                    else ""
                ),
                "created_at": _iso(c.created_at),
                "started_at": _iso(c.started_at),
                "completed_at": _iso(c.completed_at),
                "payload": c.payload or {},
                "result_payload": c.result_payload or {},
                "last_error": c.last_error or "",
            }
        )
    return Response({"count": len(results), "results": results})


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_workspaces_list(request):
    """GET /api/v1/sync/admin/workspaces/ — Booking Workspaces."""
    qs = (
        BookingWorkspace.objects.select_related("sync_agent", "booking", "equipment")
        .order_by("-updated_at")[:300]
    )
    results = [
        {
            "id": str(w.id),
            "workspace_name": w.workspace_name,
            "status": w.status,
            "relative_folder": w.relative_folder,
            "expected_result_folder": w.expected_result_folder,
            "agent_id": str(w.sync_agent_id),
            "agent_name": w.sync_agent.agent_name,
            "booking_id": w.booking_id,
            "equipment_id": w.equipment_id,
            "equipment_name": w.equipment.name if w.equipment_id else "",
            "configuration_version": w.configuration_version,
            "created_at": _iso(w.created_at),
            "updated_at": _iso(w.updated_at),
        }
        for w in qs
    ]
    return Response({"count": len(results), "results": results})


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_logs_list(request):
    """GET /api/v1/sync/admin/logs/ — Sync Logs."""
    severity = (request.query_params.get("severity") or "").strip().upper()
    agent_id = (request.query_params.get("agent_id") or "").strip()
    qs = SyncLog.objects.select_related("sync_agent", "equipment").order_by("-created_at")
    if severity:
        qs = qs.filter(severity=severity)
    if agent_id:
        qs = qs.filter(sync_agent_id=agent_id)
    rows = list(qs[:300])
    return Response({"count": len(rows), "results": [_serialize_log(row) for row in rows]})


@api_view(["GET"])
@authentication_classes(PORTAL_ADMIN_AUTH)
@permission_classes(PORTAL_ADMIN_PERM)
def portal_admin_base(request):
    """GET /api/v1/sync/admin/django-links/ — deep-links into Django Admin."""
    # Relative admin paths; frontend prefixes with getAdminBaseUrl().
    return Response(
        {
            "links": [
                {"key": "operations", "label": "Sync Operations Console", "path": "sync/syncoperationsconsole/console/"},
                {"key": "agents", "label": "Department Sync Agents", "path": "sync/departmentsyncagent/"},
                {"key": "assignments", "label": "Agent Assignments", "path": "sync/agentassignment/"},
                {"key": "profiles", "label": "Equipment Sync Profiles", "path": "sync/equipmentsyncprofile/"},
                {"key": "commands", "label": "Agent Commands", "path": "sync/agentcommand/"},
                {"key": "heartbeats", "label": "Agent Heartbeats", "path": "sync/agentheartbeat/"},
                {"key": "workspaces", "label": "Booking Workspaces", "path": "sync/bookingworkspace/"},
                {"key": "logs", "label": "Sync Logs", "path": "sync/synclog/"},
            ]
        },
        status=status.HTTP_200_OK,
    )
