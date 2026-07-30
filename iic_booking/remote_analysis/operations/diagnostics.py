"""Administrator deployment diagnostics (read-only aggregation)."""

from __future__ import annotations

import os

from django.conf import settings as django_settings
from django.db import connection
from django.db.models import Count
from django.utils import timezone
from django.utils.html import escape
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.constants import (
    CommandStatus,
    SessionStatus,
    TransferStatus,
    WorkspaceSyncPhase,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.remote_analysis.scheduler_models import ReservationQueue
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings, RemoteDesktopSession
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceTransfer

_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]


def build_diagnostics_payload() -> dict:
    now = timezone.now()
    settings_obj = RemoteAnalysisSettings.get_solo()

    db_ok = False
    db_error = ""
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        db_ok = True
    except Exception as exc:  # noqa: BLE001
        db_error = str(exc)[:200]

    guac: dict = {"mock": bool(settings_obj.mock_guacamole), "status": "unknown"}
    try:
        from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
        from iic_booking.remote_analysis.guacamole.settings_env import production_guacamole_configured

        configured, problems = production_guacamole_configured(settings_obj)
        if settings_obj.mock_guacamole:
            guac["status"] = "mock"
            if not django_settings.DEBUG:
                guac["status"] = "mock_forbidden_when_debug_false"
        elif not configured:
            guac["status"] = "misconfigured"
            guac["problems"] = problems
        elif GuacamoleClient(settings_obj).health_check():
            guac["status"] = "ok"
        else:
            guac["status"] = "unreachable"
    except Exception as exc:  # noqa: BLE001
        guac["status"] = f"error:{type(exc).__name__}"
        guac["detail"] = str(exc)[:200]

    from iic_booking.remote_analysis.workspace.storage import StorageManager

    storage = StorageManager(settings_obj)
    root = storage.workspace_root()
    archive = storage.archive_root()
    storage_info = {
        "workspace_root": str(root),
        "workspace_root_exists": root.exists(),
        "workspace_root_writable": os.access(root, os.W_OK) if root.exists() else False,
        "archive_root": str(archive),
        "archive_root_exists": archive.exists(),
        "archive_root_writable": os.access(archive, os.W_OK) if archive.exists() else False,
    }
    try:
        root.mkdir(parents=True, exist_ok=True)
        archive.mkdir(parents=True, exist_ok=True)
        storage_info["workspace_root_exists"] = root.exists()
        storage_info["workspace_root_writable"] = os.access(root, os.W_OK)
        storage_info["archive_root_exists"] = archive.exists()
        storage_info["archive_root_writable"] = os.access(archive, os.W_OK)
    except Exception as exc:  # noqa: BLE001
        storage_info["error"] = str(exc)[:200]

    workstations = []
    for ws in AnalysisWorkstation.objects.order_by("-last_heartbeat")[:100]:
        age = None
        if ws.last_heartbeat:
            age = int((now - ws.last_heartbeat).total_seconds())
        workstations.append(
            {
                "id": str(ws.id),
                "agent_id": ws.agent_id,
                "hostname": ws.hostname,
                "display_name": ws.display_name,
                "status": ws.status,
                "enabled": ws.enabled,
                "health_score": ws.health_score,
                "agent_version": getattr(ws, "agent_version", "") or "",
                "last_heartbeat": ws.last_heartbeat.isoformat() if ws.last_heartbeat else None,
                "heartbeat_age_seconds": age,
            }
        )

    active_sessions = list(
        RemoteDesktopSession.objects.filter(
            status__in=[
                SessionStatus.ACTIVE,
                SessionStatus.CONNECTED,
                SessionStatus.IDLE,
                SessionStatus.LAUNCHED,
                SessionStatus.PREPARING,
                SessionStatus.READY,
                SessionStatus.TOKEN_GENERATED,
            ]
        ).values("id", "status", "user__email", "workstation__hostname", "created_at")[:50]
    )

    queue_length = ReservationQueue.objects.filter(
        status__in=["WAITING", "ALLOCATING"]
    ).count()

    sync_counts = {
        r["sync_phase"] or "(empty)": r["c"]
        for r in AnalysisWorkspace.objects.values("sync_phase").annotate(c=Count("id"))
    }
    status_counts = {
        r["status"]: r["c"] for r in AnalysisWorkspace.objects.values("status").annotate(c=Count("id"))
    }

    cleanup_failed = AnalysisWorkspace.objects.filter(sync_phase=WorkspaceSyncPhase.CLEANUP_FAILED).count()
    upload_failed = AnalysisWorkspace.objects.filter(
        sync_phase__in=[
            WorkspaceSyncPhase.UPLOAD_FAILED,
            WorkspaceSyncPhase.RETRY_PENDING,
            WorkspaceSyncPhase.PREPARATION_FAILED,
        ]
    ).count()
    recent_failed_transfers = list(
        WorkspaceTransfer.objects.filter(status=TransferStatus.FAILED)
        .order_by("-created_at")
        .values("id", "workspace_id", "direction", "error_message", "created_at")[:20]
    )

    pending_commands = RemoteCommand.objects.filter(
        status__in=[CommandStatus.PENDING, CommandStatus.DELIVERED]
    ).count()

    celery_info: dict = {
        "broker_configured": bool(getattr(django_settings, "CELERY_BROKER_URL", "")),
        "beat_entries": [],
    }
    try:
        from django_celery_beat.models import PeriodicTask

        celery_info["beat_entries"] = list(
            PeriodicTask.objects.filter(name__startswith="RAA ").values("name", "enabled", "last_run_at")[:30]
        )
        celery_info["beat_table"] = "ok"
    except Exception as exc:  # noqa: BLE001
        celery_info["beat_table"] = f"unavailable:{type(exc).__name__}"
        celery_info["detail"] = str(exc)[:200]

    enrollment = bool((os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip())

    return {
        "generated_at": now.isoformat(),
        "django": {
            "DEBUG": bool(django_settings.DEBUG),
            "database": "ok" if db_ok else f"error:{db_error}",
            "enrollment_key_configured": enrollment,
        },
        "guacamole": guac,
        "settings": {
            "mock_guacamole": bool(settings_obj.mock_guacamole),
            "workspace_sync_mode": getattr(settings_obj, "workspace_sync_mode", ""),
            "workspace_root": settings_obj.workspace_root or "(default MEDIA)",
            "verify_tls": getattr(settings_obj, "verify_tls", True),
        },
        "storage": storage_info,
        "workstations": workstations,
        "sessions": {"active": active_sessions, "count": len(active_sessions)},
        "scheduler": {"queue_length": queue_length, "pending_commands": pending_commands},
        "workspaces": {
            "by_status": status_counts,
            "by_sync_phase": sync_counts,
            "cleanup_failures": cleanup_failed,
            "sync_failures": upload_failed,
            "recent_failed_transfers": recent_failed_transfers,
        },
        "celery": celery_info,
        "warnings": _warnings(
            django_settings.DEBUG,
            settings_obj.mock_guacamole,
            enrollment,
            guac.get("status"),
            storage_info,
        ),
    }


def _warnings(debug, mock, enrollment, guac_status, storage_info) -> list[str]:
    out = []
    if debug:
        out.append("DEBUG=True — not suitable for production")
    if mock:
        out.append("mock_guacamole=True — set RA_MOCK_GUACAMOLE=false for live RDP")
    if not enrollment and not debug:
        out.append("RA_AGENT_ENROLLMENT_KEY missing while DEBUG=False")
    if guac_status not in {"ok", "mock"}:
        out.append(f"Guacamole status: {guac_status}")
    if not storage_info.get("workspace_root_writable"):
        out.append("Workspace root not writable")
    return out


def render_diagnostics_html(payload: dict) -> str:
    warnings = "".join(f"<li>{escape(w)}</li>" for w in payload.get("warnings") or []) or "<li>None</li>"
    rows = ""
    for ws in payload.get("workstations") or []:
        rows += (
            "<tr>"
            f"<td>{escape(str(ws.get('hostname') or ''))}</td>"
            f"<td>{escape(str(ws.get('status') or ''))}</td>"
            f"<td>{escape(str(ws.get('agent_version') or ''))}</td>"
            f"<td>{escape(str(ws.get('heartbeat_age_seconds')))}</td>"
            f"<td>{escape(str(ws.get('health_score')))}</td>"
            "</tr>"
        )
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>RA Diagnostics</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;margin:1.5rem;background:#f6f7f9;color:#1a1a1a}}
h1,h2{{margin:0.6rem 0}} section{{background:#fff;border:1px solid #ddd;padding:1rem;margin:1rem 0;border-radius:6px}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #ccc;padding:0.35rem 0.5rem;text-align:left;font-size:0.9rem}}
.warn{{color:#8a1f11}} pre{{white-space:pre-wrap;font-size:0.8rem}}
</style></head><body>
<h1>Remote Analysis — Deployment Diagnostics</h1>
<p>Generated: {escape(payload.get('generated_at',''))} · Manage permission required
 · <a href="/api/v1/analysis/operations/toolkit/?view=html">Commissioning Toolkit</a>
 · <a href="/api/v1/analysis/operations/commissioning/?view=html">Sync Commissioning Console</a></p>
<section><h2>Warnings</h2><ul class="warn">{warnings}</ul></section>
<section><h2>Django / Guacamole</h2>
<pre>DEBUG={escape(str(payload['django']['DEBUG']))}
database={escape(str(payload['django']['database']))}
enrollment={escape(str(payload['django']['enrollment_key_configured']))}
guacamole={escape(str(payload['guacamole']))}
settings={escape(str(payload['settings']))}</pre></section>
<section><h2>Storage</h2><pre>{escape(str(payload['storage']))}</pre></section>
<section><h2>Scheduler</h2><pre>{escape(str(payload['scheduler']))}</pre></section>
<section><h2>Workspaces / Sync</h2><pre>{escape(str(payload['workspaces']))}</pre></section>
<section><h2>Celery Beat (RAA tasks)</h2><pre>{escape(str(payload['celery']))}</pre></section>
<section><h2>Workstations</h2>
<table><thead><tr><th>Hostname</th><th>Status</th><th>Agent version</th><th>Heartbeat age (s)</th><th>Health</th></tr></thead>
<tbody>{rows or '<tr><td colspan="5">No workstations</td></tr>'}</tbody></table></section>
<section><h2>Active sessions</h2><pre>{escape(str(payload['sessions']))}</pre></section>
</body></html>"""


@api_view(["GET"])
@permission_classes(_MANAGE)
def deployment_diagnostics(request):
    """GET /api/v1/analysis/operations/diagnostics/ — manage-only. ``?view=html`` for page."""
    payload = build_diagnostics_payload()
    # Use view=html (not format=html) — DRF reserves ?format= for content negotiation.
    if (request.query_params.get("view") or request.query_params.get("render") or "").lower() == "html":
        from django.http import HttpResponse

        return HttpResponse(render_diagnostics_html(payload), content_type="text/html; charset=utf-8")
    return Response(payload)
