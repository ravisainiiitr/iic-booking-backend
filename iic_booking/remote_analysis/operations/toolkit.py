"""
Commissioning & Diagnostics Toolkit (admin-only, optional).

Does not alter production workflows. Reuses diagnostics + commissioning helpers.
"""

from __future__ import annotations

import hashlib
import os
import time
from datetime import timedelta
from io import BytesIO
from typing import Any
from uuid import uuid4

from django.conf import settings as django_settings
from django.db import connection
from django.db.models import Q, Sum
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    HEARTBEAT_OFFLINE_SECONDS,
    CommandStatus,
    TransferDirection,
    TransferStatus,
    WorkstationStatus,
    WorkspaceStatus,
    WorkspaceSyncPhase,
)
from iic_booking.remote_analysis.models import (
    AgentToken,
    AnalysisWorkstation,
    RemoteCommand,
    WorkstationEvent,
    WorkstationHeartbeat,
)
from iic_booking.remote_analysis.operations.diagnostics import build_diagnostics_payload
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.workspace_models import (
    AnalysisWorkspace,
    WorkspaceAudit,
    WorkspaceTransfer,
)


def _timed(fn):
    t0 = time.perf_counter()
    try:
        result = fn()
        ms = int((time.perf_counter() - t0) * 1000)
        if isinstance(result, dict) and "duration_ms" not in result:
            result = {**result, "duration_ms": ms}
        return result
    except Exception as exc:  # noqa: BLE001
        ms = int((time.perf_counter() - t0) * 1000)
        return {
            "status": "FAIL",
            "duration_ms": ms,
            "error": f"{type(exc).__name__}: {exc}",
        }


def _rag(ok: bool, *, amber: bool = False) -> str:
    if amber:
        return "AMBER"
    return "GREEN" if ok else "RED"


def probe_database_latency_ms() -> dict[str, Any]:
    def _run():
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {"status": "PASS", "detail": "SELECT 1 ok"}

    return _timed(_run)


def probe_redis() -> dict[str, Any]:
    def _run():
        url = (
            getattr(django_settings, "REDIS_URL", None)
            or getattr(django_settings, "CELERY_BROKER_URL", None)
            or ""
        )
        if not url or not str(url).startswith(("redis://", "rediss://")):
            return {
                "status": "PASS",
                "detail": "Redis not configured (N/A)",
                "configured": False,
            }
        try:
            import redis  # type: ignore

            client = redis.from_url(str(url), socket_connect_timeout=2, socket_timeout=2)
            pong = client.ping()
            return {
                "status": "PASS" if pong else "FAIL",
                "detail": "PING OK" if pong else "PING failed",
                "configured": True,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "FAIL",
                "detail": str(exc)[:200],
                "configured": True,
            }

    return _timed(_run)


def probe_storage_usage() -> dict[str, Any]:
    from iic_booking.remote_analysis.workspace.storage import StorageManager

    settings_obj = RemoteAnalysisSettings.get_solo()
    storage = StorageManager(settings_obj)
    root = storage.workspace_root()
    info: dict[str, Any] = {
        "workspace_root": str(root),
        "exists": root.exists(),
        "writable": False,
        "free_bytes": None,
        "total_bytes": None,
    }
    try:
        root.mkdir(parents=True, exist_ok=True)
        info["writable"] = os.access(root, os.W_OK)
        usage = os.statvfs(root) if hasattr(os, "statvfs") else None
        if usage:
            info["free_bytes"] = usage.f_bavail * usage.f_frsize
            info["total_bytes"] = usage.f_blocks * usage.f_frsize
        elif os.name == "nt":
            import ctypes

            free = ctypes.c_ulonglong(0)
            total = ctypes.c_ulonglong(0)
            ctypes.windll.kernel32.GetDiskFreeSpaceExW(  # type: ignore[attr-defined]
                str(root),
                ctypes.byref(ctypes.c_ulonglong(0)),
                ctypes.byref(total),
                ctypes.byref(free),
            )
            info["free_bytes"] = int(free.value)
            info["total_bytes"] = int(total.value)
    except Exception as exc:  # noqa: BLE001
        info["error"] = str(exc)[:200]
    info["status"] = "PASS" if info.get("writable") else "FAIL"
    return info


def probe_guacamole() -> dict[str, Any]:
    """Guacamole connectivity, session counts, tunnel/health, API latency (toolkit)."""

    def _run():
        from django.db.models import Count

        from iic_booking.remote_analysis.constants import SessionStatus
        from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
        from iic_booking.remote_analysis.guacamole.settings_env import production_guacamole_configured
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionHealth

        settings_obj = RemoteAnalysisSettings.get_solo()
        configured, problems = production_guacamole_configured(settings_obj)
        client = GuacamoleClient(settings_obj)
        probe = client.health_probe()
        open_statuses = {
            SessionStatus.CREATED,
            SessionStatus.PREPARING,
            SessionStatus.READY,
            SessionStatus.TOKEN_GENERATED,
            SessionStatus.LAUNCHED,
            SessionStatus.CONNECTING,
            SessionStatus.CONNECTED,
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
        }
        active = RemoteDesktopSession.objects.filter(status__in=open_statuses).count()
        by_status = {
            row["status"]: row["c"]
            for row in RemoteDesktopSession.objects.values("status").annotate(c=Count("id"))
        }
        health_rows = SessionHealth.objects.order_by("-last_check_at")[:20]
        tunnel = {
            "recent_checks": [
                {
                    "session_id": str(h.session_id),
                    "guacamole_reachable": h.guacamole_reachable,
                    "agent_online": h.agent_online,
                    "score": h.score,
                    "detail": h.detail,
                    "last_check_at": h.last_check_at.isoformat() if h.last_check_at else None,
                }
                for h in health_rows
            ]
        }
        status_val = "PASS"
        if settings_obj.mock_guacamole:
            detail = "mock_guacamole=True"
        elif not configured:
            status_val = "FAIL"
            detail = "misconfigured: " + ", ".join(problems or [])
        elif not probe.get("ok"):
            status_val = "FAIL"
            detail = probe.get("error") or probe.get("status") or "unreachable"
        else:
            detail = f"ok latency_ms={probe.get('latency_ms')}"
        return {
            "status": status_val,
            "detail": detail,
            "mock": bool(settings_obj.mock_guacamole),
            "configured": configured,
            "probe": probe,
            "active_sessions": active,
            "sessions_by_status": by_status,
            "tunnel_health": tunnel,
            "connection_latency_ms": probe.get("latency_ms"),
        }

    return _timed(_run)


# ---------------------------------------------------------------------------
# 1. Portal diagnostics dashboard
# ---------------------------------------------------------------------------


def build_toolkit_dashboard() -> dict[str, Any]:
    """Enrich existing diagnostics with queue / transfer / latency signals."""
    base = build_diagnostics_payload()
    now = timezone.now()

    db = probe_database_latency_ms()
    redis_info = probe_redis()
    storage = probe_storage_usage()
    guacamole = probe_guacamole()

    online_statuses = {
        WorkstationStatus.ONLINE,
        WorkstationStatus.AVAILABLE,
        WorkstationStatus.PREPARING,
        WorkstationStatus.BUSY,
        WorkstationStatus.CLEANING,
    }
    workstations = list(AnalysisWorkstation.objects.all())
    online = []
    offline = []
    enriched = []
    for ws in workstations:
        age = int((now - ws.last_heartbeat).total_seconds()) if ws.last_heartbeat else None
        is_online = (
            ws.enabled
            and ws.status in online_statuses
            and age is not None
            and age <= HEARTBEAT_OFFLINE_SECONDS
        )
        current_cmd = (
            RemoteCommand.objects.filter(
                workstation=ws,
                status__in=[CommandStatus.PENDING, CommandStatus.DELIVERED, CommandStatus.RUNNING],
            )
            .order_by("-created_at")
            .values("id", "command_type", "status", "created_at")
            .first()
        )
        current_ws = (
            AnalysisWorkspace.objects.filter(workstation=ws)
            .exclude(status__in=[WorkspaceStatus.DELETED, WorkspaceStatus.ARCHIVED])
            .order_by("-updated_at")
            .values("id", "sync_phase", "status", "booking_id")
            .first()
        )
        row = {
            "id": str(ws.id),
            "agent_id": ws.agent_id,
            "hostname": ws.hostname,
            "status": ws.status,
            "online": is_online,
            "heartbeat_age_seconds": age,
            "health_score": ws.health_score,
            "current_command": current_cmd,
            "current_workspace": {**current_ws, "id": str(current_ws["id"])} if current_ws else None,
            "agent_version": ws.agent_version or "",
        }
        enriched.append(row)
        (online if is_online else offline).append(row)

    pending_commands = RemoteCommand.objects.filter(
        status__in=[CommandStatus.PENDING, CommandStatus.DELIVERED, CommandStatus.RUNNING]
    ).count()
    running_workspaces = AnalysisWorkspace.objects.filter(
        sync_phase__in=[
            WorkspaceSyncPhase.DOWNLOADING_INPUT,
            WorkspaceSyncPhase.VERIFYING_INPUT,
            WorkspaceSyncPhase.COLLECTING_OUTPUT,
            WorkspaceSyncPhase.UPLOADING_OUTPUT,
            WorkspaceSyncPhase.CLEANUP,
            WorkspaceSyncPhase.SESSION_ACTIVE,
            WorkspaceSyncPhase.SESSION_STARTING,
        ]
    ).count()
    failed_workspaces = AnalysisWorkspace.objects.filter(
        sync_phase__in=[
            WorkspaceSyncPhase.PREPARATION_FAILED,
            WorkspaceSyncPhase.UPLOAD_FAILED,
            WorkspaceSyncPhase.CLEANUP_FAILED,
        ]
    ).count()
    retry_agg = WorkspaceTransfer.objects.aggregate(total=Sum("retry_count"))
    active_statuses = [
        TransferStatus.PENDING,
        TransferStatus.IN_PROGRESS,
        TransferStatus.RETRYING,
    ]
    active_uploads = WorkspaceTransfer.objects.filter(
        direction__in=[TransferDirection.AGENT_PUSH, TransferDirection.WORKSPACE_TO_PORTAL],
        status__in=active_statuses,
    ).count()
    active_downloads = WorkspaceTransfer.objects.filter(
        direction__in=[TransferDirection.AGENT_PULL, TransferDirection.PORTAL_TO_WORKSPACE],
        status__in=active_statuses,
    ).count()

    overview = {
        "workstations_total": len(workstations),
        "workstations_online": len(online),
        "workstations_offline": len(offline),
        "pending_commands": pending_commands,
        "running_workspaces": running_workspaces,
        "failed_workspaces": failed_workspaces,
        "retry_count_sum": int(retry_agg["total"] or 0),
        "active_uploads": active_uploads,
        "active_downloads": active_downloads,
        "database_latency_ms": db.get("duration_ms"),
        "database": db,
        "redis": redis_info,
        "storage": storage,
        "guacamole": guacamole,
    }

    return {
        "generated_at": now.isoformat(),
        "overview": overview,
        "workstations": enriched,
        "diagnostics": base,
        "guacamole": guacamole,
        "links": {
            "commissioning": "/api/v1/analysis/operations/commissioning/?view=html",
            "legacy_diagnostics": "/api/v1/analysis/operations/diagnostics/?view=html",
            "toolkit": "/api/v1/analysis/operations/toolkit/?view=html",
        },
    }


# ---------------------------------------------------------------------------
# 2. Agent diagnostics (portal view of agent)
# ---------------------------------------------------------------------------


def build_agent_diagnostics(workstation_id: str | None = None, agent_id: str | None = None) -> dict[str, Any]:
    qs = AnalysisWorkstation.objects.all()
    if workstation_id:
        ws = qs.filter(pk=workstation_id).first()
    elif agent_id:
        ws = qs.filter(agent_id=agent_id).first()
    else:
        ws = qs.order_by("-last_heartbeat").first()
    if ws is None:
        return {"status": "FAIL", "error": "No workstation found"}

    now = timezone.now()
    age = int((now - ws.last_heartbeat).total_seconds()) if ws.last_heartbeat else None
    hb = WorkstationHeartbeat.objects.filter(workstation=ws).order_by("-received_at").first()
    token = AgentToken.objects.filter(workstation=ws, is_active=True, revoked_at__isnull=True).order_by("-issued_at").first()
    last_cmd = RemoteCommand.objects.filter(workstation=ws).order_by("-created_at").first()
    last_upload = (
        WorkspaceTransfer.objects.filter(
            workspace__workstation=ws,
            direction__in=[TransferDirection.AGENT_PUSH, TransferDirection.WORKSPACE_TO_PORTAL],
            status=TransferStatus.COMPLETED,
        )
        .order_by("-created_at")
        .first()
    )
    last_download = (
        WorkspaceTransfer.objects.filter(
            workspace__workstation=ws,
            direction__in=[TransferDirection.AGENT_PULL, TransferDirection.PORTAL_TO_WORKSPACE],
            status=TransferStatus.COMPLETED,
        )
        .order_by("-created_at")
        .first()
    )
    settings_obj = RemoteAnalysisSettings.get_solo()
    inventory = getattr(ws, "inventory", None)

    return {
        "generated_at": now.isoformat(),
        "workstation": {
            "id": str(ws.id),
            "agent_id": ws.agent_id,
            "hostname": ws.hostname,
            "display_name": ws.display_name,
            "status": ws.status,
            "enabled": ws.enabled,
            "health_score": ws.health_score,
            "agent_version": ws.agent_version or "",
            "os": ws.operating_system or ws.windows_version or (
                hb.raw_payload.get("os") if hb and isinstance(hb.raw_payload, dict) else ""
            ),
            "cpu_cores": ws.cpu_cores,
            "memory_gb": ws.memory_gb,
            "storage_gb": ws.storage_gb,
            "heartbeat_age_seconds": age,
        },
        "machine": {
            "cpu_percent": hb.cpu if hb else None,
            "memory_percent": hb.memory if hb else None,
            "disk_percent": hb.disk if hb else None,
            "windows_uptime_hours": hb.windows_uptime_hours if hb else None,
            "logged_in_user": hb.logged_in_user if hb else "",
            "portal_latency_ms": hb.portal_latency_ms if hb else None,
            "hardware": (inventory.hardware_json if inventory else {}) or {},
        },
        "configuration": {
            "enrollment_key_configured": bool((os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip()),
            "workspace_root_portal": settings_obj.workspace_root or "(default MEDIA)",
            "agent_sessions_hint": r"C:\ProgramData\RemoteAnalysisAgent\Sessions\<reservation_id>",
            "agent_logs_hint": r"C:\ProgramData\RemoteAnalysisAgent\Logs\raa-*.log",
            "agent_state_hint": r"C:\ProgramData\RemoteAnalysisAgent\State\agent-state.json",
            "local_health_hint": "http://127.0.0.1:<LocalHealthPort>/api/health (loopback on agent PC)",
            "heartbeat_interval_hint_seconds": 30,
        },
        "token": {
            "present": bool(token),
            "prefix": token.token_prefix if token else "",
            "issued_at": token.issued_at.isoformat() if token else None,
            "expires_at": token.expires_at.isoformat() if token and token.expires_at else None,
            "last_used_at": token.last_used_at.isoformat() if token and token.last_used_at else None,
            "expired": bool(token and token.expires_at and token.expires_at < now),
        },
        "last_command": {
            "id": str(last_cmd.id) if last_cmd else None,
            "type": last_cmd.command_type if last_cmd else None,
            "status": last_cmd.status if last_cmd else None,
            "created_at": last_cmd.created_at.isoformat() if last_cmd else None,
            "completed_at": last_cmd.completed_at.isoformat() if last_cmd and last_cmd.completed_at else None,
        },
        "last_successful_upload": {
            "id": str(last_upload.id) if last_upload else None,
            "created_at": last_upload.created_at.isoformat() if last_upload else None,
        },
        "last_successful_download": {
            "id": str(last_download.id) if last_download else None,
            "created_at": last_download.created_at.isoformat() if last_download else None,
        },
        "note": (
            "Live OS counters come from the latest portal heartbeat. "
            "Agent loopback health is only reachable on the Analysis PC itself."
        ),
    }


# ---------------------------------------------------------------------------
# 3. Connectivity tests
# ---------------------------------------------------------------------------


def run_connectivity_tests(
    *,
    actor=None,
    workstation_id: str | None = None,
    commissioning_run_id: str | None = None,
) -> dict[str, Any]:
    from iic_booking.remote_analysis.operations.commissioning_observability import (
        STEP_CONNECTIVITY,
        STEP_TOOLKIT_STARTED,
        bind_run_context,
        complete_run,
        end_step,
        begin_step,
        get_run,
        link_workspace,
        persist_evidence_bundle,
        start_commissioning_run,
    )

    run = get_run(commissioning_run_id) if commissioning_run_id else None
    if run is None:
        run = start_commissioning_run(actor=actor, workstation_id=workstation_id)
    begin_step(run, STEP_TOOLKIT_STARTED)
    end_step(run, STEP_TOOLKIT_STARTED, success=True)
    begin_step(run, STEP_CONNECTIVITY)

    results: dict[str, Any] = {}
    ws = None
    if workstation_id:
        ws = AnalysisWorkstation.objects.filter(pk=workstation_id).first()
    if ws is None:
        ws = (
            AnalysisWorkstation.objects.filter(enabled=True)
            .exclude(status__in=[WorkstationStatus.DISABLED, WorkstationStatus.MAINTENANCE])
            .order_by("-last_heartbeat")
            .first()
        )

    with bind_run_context(run):
        results["portal_api"] = _timed(lambda: {"status": "PASS", "detail": "Toolkit reachable"})
        results["authentication"] = _timed(
            lambda: {
                "status": "PASS" if actor and getattr(actor, "is_authenticated", False) else "FAIL",
                "detail": f"user={getattr(actor, 'email', None) or getattr(actor, 'pk', None)}",
            }
        )
        results["database"] = probe_database_latency_ms()
        results["redis"] = probe_redis()

        def _storage():
            info = probe_storage_usage()
            return {**info, "status": info.get("status") or "FAIL"}

        results["storage"] = _timed(_storage)

        results["guacamole"] = probe_guacamole()

        def _hb():
            if ws is None:
                return {"status": "FAIL", "detail": "No workstation"}
            if not ws.last_heartbeat:
                return {"status": "FAIL", "detail": "Never heartbeated"}
            age = int((timezone.now() - ws.last_heartbeat).total_seconds())
            ok = age <= HEARTBEAT_OFFLINE_SECONDS
            return {
                "status": "PASS" if ok else "FAIL",
                "detail": f"age={age}s workstation={ws.hostname}",
                "workstation_id": str(ws.id),
            }

        results["heartbeat"] = _timed(_hb)

        workspace = None
        uploaded = None

        def _workspace_create():
            nonlocal workspace
            if ws is None:
                return {"status": "FAIL", "detail": "No workstation for workspace create"}
            from iic_booking.remote_analysis.services.reservation import ReservationService
            from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

            start = timezone.now()
            reservation = ReservationService().create_reservation(
                user=actor,
                requested_start=start,
                requested_end=start + timedelta(hours=1),
                created_by=actor,
                auto_allocate=False,
            )
            reservation.workstation = ws
            reservation.save(update_fields=["workstation", "updated_at"])
            workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=actor, ingest=False)
            workspace.workstation = ws
            workspace.save(update_fields=["workstation", "updated_at"])
            link_workspace(run, workspace)
            return {
                "status": "PASS",
                "detail": f"workspace={workspace.id}",
                "workspace_id": str(workspace.id),
            }

        results["workspace_creation"] = _timed(_workspace_create)

        def _upload():
            nonlocal uploaded
            from django.core.files.uploadedfile import SimpleUploadedFile

            from iic_booking.remote_analysis.workspace.transfer import TransferManager

            if workspace is None:
                return {"status": "FAIL", "detail": "No workspace"}
            payload = b"toolkit-connectivity-" + uuid4().hex.encode()
            uploaded = TransferManager().upload(
                workspace,
                SimpleUploadedFile("toolkit-probe.txt", payload, content_type="text/plain"),
                folder="RawData",
                actor=actor,
            )
            return {
                "status": "PASS",
                "detail": f"file={uploaded.relative_path} sha={uploaded.sha256[:12]}…",
                "sha256": uploaded.sha256,
                "file_id": str(uploaded.id),
            }

        results["file_upload"] = _timed(_upload)

        def _download():
            from iic_booking.remote_analysis.workspace.transfer import TransferManager

            if workspace is None or uploaded is None:
                return {"status": "FAIL", "detail": "Missing workspace/file"}
            resp = TransferManager().download_file(workspace, uploaded, actor=actor)
            content = b"".join(resp.streaming_content) if hasattr(resp, "streaming_content") else resp.content
            digest = hashlib.sha256(content).hexdigest()
            ok = digest == uploaded.sha256
            return {
                "status": "PASS" if ok else "FAIL",
                "detail": f"bytes={len(content)} checksum_match={ok}",
                "sha256": digest,
            }

        results["file_download"] = _timed(_download)

        def _cleanup():
            from iic_booking.remote_analysis.operations.commissioning import cleanup_workspace
            from iic_booking.remote_analysis.workspace.transfer import TransferManager

            if workspace is None:
                return {"status": "FAIL", "detail": "No workspace"}
            if uploaded is not None:
                try:
                    TransferManager().soft_delete(workspace, uploaded, actor=actor)
                except Exception:  # noqa: BLE001
                    pass
            cmd = cleanup_workspace(workspace_id=str(workspace.id), actor=actor)
            return {
                "status": "PASS",
                "detail": f"cleanup_command={getattr(cmd, 'id', None)}",
                "command_id": str(cmd.id) if cmd else None,
            }

        results["cleanup"] = _timed(_cleanup)

    passed = sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "PASS")
    failed = sum(1 for r in results.values() if isinstance(r, dict) and r.get("status") == "FAIL")
    overall = "PASS" if failed == 0 else "FAIL"
    end_step(
        run,
        STEP_CONNECTIVITY,
        success=failed == 0,
        error="" if failed == 0 else f"{failed} checks failed",
        meta={"summary": {"pass": passed, "fail": failed}},
    )
    complete_run(run, success=failed == 0, summary={"connectivity": overall})
    evidence_path = ""
    try:
        evidence_path = persist_evidence_bundle(run)
    except Exception:  # noqa: BLE001
        evidence_path = ""

    return {
        "generated_at": timezone.now().isoformat(),
        "commissioning_run_id": str(run.id),
        "evidence_path": evidence_path,
        "evidence_url": f"/api/v1/analysis/operations/toolkit/runs/{run.id}/evidence/",
        "workstation_id": str(ws.id) if ws else None,
        "summary": {"pass": passed, "fail": failed, "total": passed + failed},
        "results": results,
        "overall": overall,
    }


# ---------------------------------------------------------------------------
# 4. Log viewer (portal-side operational logs)
# ---------------------------------------------------------------------------


def query_ops_logs(
    *,
    workspace_id: str | None = None,
    booking_id: str | None = None,
    workstation_id: str | None = None,
    severity: str | None = None,
    search: str | None = None,
    since_hours: int = 24,
    limit: int = 200,
) -> dict[str, Any]:
    since = timezone.now() - timedelta(hours=max(1, min(since_hours, 24 * 30)))
    limit = max(1, min(int(limit), 500))
    rows: list[dict[str, Any]] = []

    events = WorkstationEvent.objects.filter(created_at__gte=since).select_related("workstation", "actor")
    if workstation_id:
        events = events.filter(workstation_id=workstation_id)
    if search:
        events = events.filter(Q(details__icontains=search) | Q(action__icontains=search) | Q(category__icontains=search))
    if severity == "error":
        events = events.filter(success=False)
    elif severity == "info":
        events = events.filter(success=True)
    for e in events.order_by("-created_at")[:limit]:
        rows.append(
            {
                "source": "portal_event",
                "severity": "error" if not e.success else "info",
                "created_at": e.created_at.isoformat(),
                "workstation_id": str(e.workstation_id) if e.workstation_id else None,
                "hostname": e.workstation.hostname if e.workstation_id else None,
                "category": e.category,
                "action": e.action,
                "details": e.details[:500],
                "actor": getattr(e.actor, "email", None),
            }
        )

    audits = WorkspaceAudit.objects.filter(created_at__gte=since).select_related("workspace", "actor")
    if workspace_id:
        audits = audits.filter(workspace_id=workspace_id)
    if booking_id:
        audits = audits.filter(workspace__booking_id=booking_id)
    if workstation_id:
        audits = audits.filter(workspace__workstation_id=workstation_id)
    if search:
        audits = audits.filter(Q(details__icontains=search) | Q(action__icontains=search))
    if severity == "error":
        audits = audits.filter(success=False)
    for a in audits.order_by("-created_at")[:limit]:
        rows.append(
            {
                "source": "workspace_audit",
                "severity": "error" if not getattr(a, "success", True) else "info",
                "created_at": a.created_at.isoformat(),
                "workspace_id": str(a.workspace_id) if a.workspace_id else None,
                "booking_id": getattr(a.workspace, "booking_id", None) if a.workspace_id else None,
                "action": a.action,
                "details": (a.details or "")[:500],
                "actor": getattr(a.actor, "email", None),
            }
        )

    cmds = RemoteCommand.objects.filter(created_at__gte=since).select_related("workstation")
    if workstation_id:
        cmds = cmds.filter(workstation_id=workstation_id)
    if search:
        cmds = cmds.filter(
            Q(command_type__icontains=search)
            | Q(result_message__icontains=search)
            | Q(error_message__icontains=search)
        )
    if severity == "error":
        cmds = cmds.filter(status=CommandStatus.FAILED)
    for c in cmds.order_by("-created_at")[:limit]:
        sev = "error" if c.status == CommandStatus.FAILED else "info"
        rows.append(
            {
                "source": "command",
                "severity": sev,
                "created_at": c.created_at.isoformat(),
                "workstation_id": str(c.workstation_id),
                "hostname": c.workstation.hostname if c.workstation_id else None,
                "action": c.command_type,
                "details": (c.error_message or c.result_message or c.status)[:500],
                "status": c.status,
            }
        )

    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    rows = rows[:limit]
    return {
        "generated_at": timezone.now().isoformat(),
        "count": len(rows),
        "filters": {
            "workspace_id": workspace_id,
            "booking_id": booking_id,
            "workstation_id": workstation_id,
            "severity": severity,
            "search": search,
            "since_hours": since_hours,
        },
        "entries": rows,
        "note": (
            "Portal operational logs only. Agent file logs live on the Analysis PC under "
            r"C:\ProgramData\RemoteAnalysisAgent\Logs\ — use COLLECT_LOGS or RDP/share to retrieve."
        ),
    }


# ---------------------------------------------------------------------------
# 5. Health report
# ---------------------------------------------------------------------------


def build_health_report() -> dict[str, Any]:
    dash = build_toolkit_dashboard()
    ov = dash["overview"]
    db_ok = ov["database"].get("status") == "PASS"
    redis = ov["redis"]
    redis_ok = redis.get("status") == "PASS"
    storage_ok = ov["storage"].get("status") == "PASS"
    online = ov["workstations_online"]
    total = ov["workstations_total"]
    hb_ok = online > 0 or total == 0
    sync_ok = ov["failed_workspaces"] == 0
    auth_ok = bool((os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip()) or bool(django_settings.DEBUG)

    components = {
        "portal": _rag(True),
        "agent": _rag(online > 0, amber=(total > 0 and online == 0)),
        "database": _rag(db_ok),
        "redis": _rag(redis_ok, amber=not redis.get("configured", True)),
        "storage": _rag(storage_ok),
        "remote_analysis": _rag(total > 0, amber=total == 0),
        "heartbeat": _rag(hb_ok, amber=total > 0 and online == 0),
        "authentication": _rag(auth_ok, amber=not auth_ok and django_settings.DEBUG),
        "synchronization": _rag(sync_ok, amber=ov["running_workspaces"] > 0 and ov["failed_workspaces"] == 0),
        "workspace_engine": _rag(sync_ok and storage_ok),
    }
    if "RED" in components.values():
        overall = "RED"
    elif "AMBER" in components.values():
        overall = "AMBER"
    else:
        overall = "GREEN"

    return {
        "generated_at": timezone.now().isoformat(),
        "overall": overall,
        "components": components,
        "overview": ov,
        "warnings": (dash.get("diagnostics") or {}).get("warnings") or [],
    }


# ---------------------------------------------------------------------------
# 6. Self-test
# ---------------------------------------------------------------------------


def run_full_self_test(
    *,
    actor=None,
    workstation_id: str | None = None,
    commissioning_run_id: str | None = None,
) -> dict[str, Any]:
    """
    Portal-side end-to-end probe (admin-only).

    Exercises storage + workspace APIs without requiring Guacamole.
    Agent prepare/collect are queued when a live workstation is selected;
    file path verification against agent disk remains a lab step.
    """
    from django.core.files.uploadedfile import SimpleUploadedFile

    from iic_booking.remote_analysis.operations.commissioning import cleanup_workspace
    from iic_booking.remote_analysis.operations.commissioning_observability import (
        STEP_CHECKSUM_VERIFICATION,
        STEP_CLEANUP_FINISHED,
        STEP_CLEANUP_STARTED,
        STEP_INPUT_UPLOAD_FINISHED,
        STEP_INPUT_UPLOAD_STARTED,
        STEP_OUTPUT_COLLECTION_FINISHED,
        STEP_OUTPUT_COLLECTION_STARTED,
        STEP_SELF_TEST,
        STEP_TOOLKIT_STARTED,
        STEP_WORKSPACE_CREATED,
        bind_run_context,
        complete_run,
        begin_step,
        end_step,
        get_run,
        link_workspace,
        persist_evidence_bundle,
        start_commissioning_run,
    )
    from iic_booking.remote_analysis.services.reservation import ReservationService
    from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
    from iic_booking.remote_analysis.workspace.transfer import TransferManager

    run = get_run(commissioning_run_id) if commissioning_run_id else None
    if run is None:
        run = start_commissioning_run(actor=actor, workstation_id=workstation_id)
    begin_step(run, STEP_TOOLKIT_STARTED)
    end_step(run, STEP_TOOLKIT_STARTED, success=True)
    begin_step(run, STEP_SELF_TEST)

    steps: list[dict[str, Any]] = []
    report: dict[str, Any] = {
        "generated_at": timezone.now().isoformat(),
        "operator": getattr(actor, "email", None) or str(getattr(actor, "pk", "")),
        "commissioning_run_id": str(run.id),
        "steps": steps,
    }

    def add(name: str, result: dict[str, Any]):
        steps.append({"name": name, **result})

    with bind_run_context(run):
        add("portal_api", _timed(lambda: {"status": "PASS", "detail": "ok"}))
        add(
            "authentication",
            _timed(
                lambda: {
                    "status": "PASS" if actor and actor.is_authenticated else "FAIL",
                    "detail": str(getattr(actor, "email", "")),
                }
            ),
        )

        ws = None
        if workstation_id:
            ws = AnalysisWorkstation.objects.filter(pk=workstation_id).first()
        if ws is None:
            ws = AnalysisWorkstation.objects.filter(enabled=True).order_by("-last_heartbeat").first()

        workspace = None
        input_file = None
        output_file = None
        mgr = TransferManager()
        payload = b"ra-self-test-" + uuid4().hex.encode()
        expected_sha = hashlib.sha256(payload).hexdigest()

        def _create():
            nonlocal workspace
            if ws is None:
                return {"status": "FAIL", "detail": "No workstation registered"}
            start = timezone.now()
            reservation = ReservationService().create_reservation(
                user=actor,
                requested_start=start,
                requested_end=start + timedelta(hours=1),
                created_by=actor,
                auto_allocate=False,
            )
            reservation.workstation = ws
            reservation.save(update_fields=["workstation", "updated_at"])
            workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=actor, ingest=False)
            workspace.workstation = ws
            workspace.save(update_fields=["workstation", "updated_at"])
            link_workspace(run, workspace)
            end_step(run, STEP_WORKSPACE_CREATED, success=True, meta={"workspace_id": str(workspace.id)})
            return {"status": "PASS", "detail": f"workspace={workspace.id}", "workspace_id": str(workspace.id)}

        begin_step(run, STEP_WORKSPACE_CREATED)
        add("create_test_workspace", _timed(_create))

        def _upload_in():
            nonlocal input_file
            if workspace is None:
                return {"status": "FAIL", "detail": "no workspace"}
            begin_step(run, STEP_INPUT_UPLOAD_STARTED)
            input_file = mgr.upload(
                workspace,
                SimpleUploadedFile("self-test-input.txt", payload, content_type="text/plain"),
                folder="RawData",
                actor=actor,
            )
            end_step(run, STEP_INPUT_UPLOAD_STARTED, success=True)
            end_step(
                run,
                STEP_INPUT_UPLOAD_FINISHED,
                success=True,
                meta={"sha256": input_file.sha256, "path": input_file.relative_path},
            )
            return {"status": "PASS", "sha256": input_file.sha256, "path": input_file.relative_path}

        add("upload_test_file", _timed(_upload_in))

        def _download_in():
            if workspace is None or input_file is None:
                return {"status": "FAIL", "detail": "missing"}
            resp = mgr.download_file(workspace, input_file, actor=actor)
            content = b"".join(resp.streaming_content) if hasattr(resp, "streaming_content") else resp.content
            digest = hashlib.sha256(content).hexdigest()
            ok = digest == expected_sha == input_file.sha256
            return {"status": "PASS" if ok else "FAIL", "sha256": digest, "match": ok}

        add("download_test_file", _timed(_download_in))
        checksum_ok = bool(input_file and input_file.sha256 == expected_sha)
        end_step(run, STEP_CHECKSUM_VERIFICATION, success=checksum_ok, meta={"expected": expected_sha})
        add(
            "verify_checksum",
            {
                "status": "PASS" if checksum_ok else "FAIL",
                "duration_ms": 0,
                "expected": expected_sha,
                "actual": input_file.sha256 if input_file else None,
            },
        )

        def _dummy_out():
            nonlocal output_file
            if workspace is None:
                return {"status": "FAIL", "detail": "no workspace"}
            begin_step(run, STEP_OUTPUT_COLLECTION_STARTED)
            out = b"self-test-output-" + uuid4().hex.encode()
            output_file = mgr.upload(
                workspace,
                SimpleUploadedFile("self-test-output.txt", out, content_type="text/plain"),
                folder="Processed",
                actor=actor,
            )
            end_step(run, STEP_OUTPUT_COLLECTION_STARTED, success=True)
            end_step(
                run,
                STEP_OUTPUT_COLLECTION_FINISHED,
                success=True,
                meta={"path": output_file.relative_path, "sha256": output_file.sha256},
            )
            return {"status": "PASS", "path": output_file.relative_path, "sha256": output_file.sha256}

        add("generate_dummy_output", _timed(_dummy_out))
        add(
            "upload_output",
            {
                "status": "PASS" if output_file else "FAIL",
                "duration_ms": 0,
                "detail": "Portal Processed/ upload (agent collect not required for toolkit self-test)",
                "file_id": str(output_file.id) if output_file else None,
            },
        )

        def _cleanup():
            if workspace is None:
                return {"status": "FAIL", "detail": "no workspace"}
            begin_step(run, STEP_CLEANUP_STARTED)
            for f in (input_file, output_file):
                if f is not None:
                    try:
                        mgr.soft_delete(workspace, f, actor=actor)
                    except Exception:  # noqa: BLE001
                        pass
            cmd = cleanup_workspace(workspace_id=str(workspace.id), actor=actor)
            workspace.sync_phase = WorkspaceSyncPhase.COMPLETED
            workspace.status = WorkspaceStatus.READY
            workspace.save(update_fields=["sync_phase", "status", "updated_at"])
            end_step(run, STEP_CLEANUP_STARTED, success=True)
            end_step(run, STEP_CLEANUP_FINISHED, success=True, meta={"command_id": str(cmd.id) if cmd else None})
            return {"status": "PASS", "command_id": str(cmd.id) if cmd else None}

        add("cleanup", _timed(_cleanup))

    failed = [s for s in steps if s.get("status") == "FAIL"]
    ok = not failed
    end_step(run, STEP_SELF_TEST, success=ok, error="" if ok else f"{len(failed)} steps failed")
    complete_run(run, success=ok, summary={"self_test": "PASS" if ok else "FAIL"})
    evidence_path = ""
    try:
        evidence_path = persist_evidence_bundle(run)
    except Exception:  # noqa: BLE001
        evidence_path = ""

    report["overall"] = "PASS" if ok else "FAIL"
    report["summary"] = {
        "pass": sum(1 for s in steps if s.get("status") == "PASS"),
        "fail": len(failed),
        "total": len(steps),
    }
    report["workstation_id"] = str(ws.id) if ws else None
    report["workspace_id"] = str(workspace.id) if workspace else None
    report["health"] = build_health_report()
    report["evidence_path"] = evidence_path
    report["evidence_url"] = f"/api/v1/analysis/operations/toolkit/runs/{run.id}/evidence/"
    return report


# ---------------------------------------------------------------------------
# 7. Commissioning report PDF
# ---------------------------------------------------------------------------


def build_commissioning_report_payload(*, actor=None, self_test: dict | None = None) -> dict[str, Any]:
    health = build_health_report()
    dash = build_toolkit_dashboard()
    return {
        "title": "Remote Analysis Commissioning Report",
        "generated_at": timezone.now().isoformat(),
        "operator": getattr(actor, "email", None) or "",
        "installation": {
            "portal_debug": bool(django_settings.DEBUG),
            "enrollment_key_configured": bool((os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip()),
            "frontend_url": getattr(django_settings, "FRONTEND_URL", ""),
        },
        "versions": {
            "django": getattr(django_settings, "DJANGO_VERSION", ""),
            "workstations": [
                {"hostname": w["hostname"], "agent_version": w.get("agent_version"), "status": w["status"]}
                for w in dash.get("workstations") or []
            ],
        },
        "health": health,
        "overview": dash.get("overview"),
        "self_test": self_test,
        "signature": {"operator": "", "date": "", "approved": False},
    }


def render_commissioning_report_pdf(payload: dict[str, Any]) -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 40

    def line(text: str, *, bold: bool = False, size: int = 10):
        nonlocal y
        if y < 50:
            c.showPage()
            y = height - 40
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(40, y, str(text)[:110])
        y -= 14

    line(payload.get("title") or "Commissioning Report", bold=True, size=14)
    line(f"Generated: {payload.get('generated_at')}")
    line(f"Operator: {payload.get('operator')}")
    y -= 6
    line("Overall health: " + str((payload.get("health") or {}).get("overall")), bold=True)
    for name, status in ((payload.get("health") or {}).get("components") or {}).items():
        line(f"  {name}: {status}")
    y -= 6
    line("Installation", bold=True)
    for k, v in (payload.get("installation") or {}).items():
        line(f"  {k}: {v}")
    y -= 6
    line("Self-test", bold=True)
    st = payload.get("self_test") or {}
    line(f"  overall: {st.get('overall', 'n/a')}")
    for step in st.get("steps") or []:
        line(f"  - {step.get('name')}: {step.get('status')} ({step.get('duration_ms')}ms)")
    y -= 10
    line("Signature", bold=True)
    line("  Operator: ______________________  Date: ______________")
    line("  Approved: ☐ Ready for production")
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 8. Monitoring recommendations (also in docs)
# ---------------------------------------------------------------------------

MONITORING_RECOMMENDATIONS = [
    {
        "id": "offline_workstation",
        "title": "Offline workstation",
        "condition": f"heartbeat_age_seconds > {HEARTBEAT_OFFLINE_SECONDS}",
        "severity": "critical",
    },
    {
        "id": "heartbeat_timeout",
        "title": "Heartbeat timeout",
        "condition": "no WorkstationHeartbeat in interval",
        "severity": "critical",
    },
    {
        "id": "upload_failure",
        "title": "Upload failure",
        "condition": "WorkspaceTransfer UPLOAD FAILED or sync_phase=UploadFailed",
        "severity": "high",
    },
    {
        "id": "download_failure",
        "title": "Download failure",
        "condition": "WorkspaceTransfer DOWNLOAD FAILED or PreparationFailed",
        "severity": "high",
    },
    {
        "id": "workspace_stuck",
        "title": "Workspace stuck",
        "condition": "sync_phase in transit > 30 minutes without progress",
        "severity": "high",
    },
    {
        "id": "cleanup_failure",
        "title": "Cleanup failure",
        "condition": "sync_phase=CleanupFailed or CLEAN command FAILED",
        "severity": "high",
    },
    {
        "id": "token_expiry",
        "title": "Token expiry warning",
        "condition": "AgentToken.expires_at within 7 days",
        "severity": "medium",
    },
    {
        "id": "disk_space",
        "title": "Disk space warning",
        "condition": "heartbeat disk >= 90% or portal storage free < 10%",
        "severity": "high",
    },
]
