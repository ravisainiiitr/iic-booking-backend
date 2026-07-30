"""
Commissioning run observability (engineering support).

Provides CommissioningRunId, timeline steps, failure snapshots, and evidence ZIP.
Does not change the commissioning workflow or permissions.
"""

from __future__ import annotations

import io
import json
import logging
import zipfile
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timedelta
from typing import Any, Iterator

from django.conf import settings as django_settings
from django.core.files.storage import default_storage
from django.utils import timezone

from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand, WorkstationEvent
from iic_booking.remote_analysis.operations_models import (
    CommissioningFailureSnapshot,
    CommissioningRun,
    CommissioningRunStatus,
    CommissioningRunStep,
)
from iic_booking.remote_analysis.production_hardening import get_correlation_id, json_safe, structured_log
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceAudit, WorkspaceFile

logger = logging.getLogger("remote_analysis.commissioning")

_run_id: ContextVar[str | None] = ContextVar("ra_commissioning_run_id", default=None)

# Canonical timeline step names (stable for reports / SAT)
STEP_RUN_STARTED = "RunStarted"
STEP_TOOLKIT_STARTED = "ToolkitStarted"
STEP_CONNECTIVITY = "ConnectivityTests"
STEP_SELF_TEST = "SelfTest"
STEP_BOOKING_SELECTED = "BookingSelected"
STEP_WORKSPACE_CREATED = "WorkspaceCreated"
STEP_INPUT_UPLOAD_STARTED = "InputUploadStarted"
STEP_INPUT_UPLOAD_FINISHED = "InputUploadFinished"
STEP_AGENT_DOWNLOAD_STARTED = "AgentDownloadStarted"
STEP_AGENT_DOWNLOAD_FINISHED = "AgentDownloadFinished"
STEP_INPUT_VERIFICATION = "InputVerification"
STEP_ANALYSIS_STARTED = "AnalysisStarted"
STEP_ANALYSIS_FINISHED = "AnalysisFinished"
STEP_OUTPUT_COLLECTION_STARTED = "OutputCollectionStarted"
STEP_OUTPUT_COLLECTION_FINISHED = "OutputCollectionFinished"
STEP_CHECKSUM_VERIFICATION = "ChecksumVerification"
STEP_CLEANUP_STARTED = "CleanupStarted"
STEP_CLEANUP_FINISHED = "CleanupFinished"
STEP_RUN_COMPLETED = "RunCompleted"


def get_commissioning_run_id() -> str | None:
    return _run_id.get()


def set_commissioning_run_id(run_id: str | None):
    return _run_id.set(run_id)


def reset_commissioning_run_id(token) -> None:
    _run_id.reset(token)


@contextmanager
def commissioning_run_scope(run_id: str | None) -> Iterator[str | None]:
    token = _run_id.set(run_id)
    try:
        yield run_id
    finally:
        _run_id.reset(token)


def start_commissioning_run(*, actor=None, workstation_id: str | None = None, notes: str = "") -> CommissioningRun:
    ws = None
    if workstation_id:
        ws = AnalysisWorkstation.objects.filter(pk=workstation_id).first()
    run = CommissioningRun.objects.create(
        operator=actor if actor is not None and getattr(actor, "pk", None) else None,
        workstation=ws,
        notes=notes or "",
        summary={"portal_sha_hint": getattr(django_settings, "GIT_SHA", "") or ""},
    )
    mark_step(run, STEP_RUN_STARTED, success=True, meta={"source": "start_commissioning_run"})
    structured_log(
        logging.INFO,
        "CommissioningRunStarted",
        commissioning_run_id=str(run.id),
        workstation_id=str(ws.id) if ws else None,
    )
    return run


def bind_run_context(run: CommissioningRun | str | None):
    """Return a context manager binding Run ID for the current request/thread."""
    rid = str(run.id) if isinstance(run, CommissioningRun) else (str(run) if run else None)
    return commissioning_run_scope(rid)


def get_run(run_id: str) -> CommissioningRun | None:
    return CommissioningRun.objects.filter(pk=run_id).first()


def link_workspace(run: CommissioningRun, workspace: AnalysisWorkspace | None, *, booking_id: int | None = None) -> None:
    if workspace is None:
        return
    update_fields = ["workspace"]
    run.workspace = workspace
    if booking_id is not None:
        run.booking_id = booking_id
        update_fields.append("booking_id")
    elif getattr(workspace, "booking_id", None):
        run.booking_id = workspace.booking_id
        update_fields.append("booking_id")
    if workspace.workstation_id and run.workstation_id is None:
        run.workstation_id = workspace.workstation_id
        update_fields.append("workstation_id")
    run.save(update_fields=update_fields)


def mark_step(
    run: CommissioningRun | str,
    name: str,
    *,
    success: bool | None = None,
    error: str = "",
    retry_count: int | None = None,
    meta: dict | None = None,
    started_at=None,
    ended_at=None,
    duration_ms: int | None = None,
) -> CommissioningRunStep:
    if isinstance(run, str):
        run_obj = get_run(run)
        if run_obj is None:
            raise ValueError(f"Unknown commissioning run: {run}")
        run = run_obj

    now = timezone.now()
    step, _ = CommissioningRunStep.objects.get_or_create(run=run, name=name, defaults={})
    if started_at is not None:
        step.started_at = started_at
    elif step.started_at is None and success is None:
        step.started_at = now
    elif step.started_at is None and success is not None:
        step.started_at = step.started_at or now

    if success is not None:
        step.ended_at = ended_at or now
        if step.started_at and step.ended_at and duration_ms is None:
            step.duration_ms = max(0, int((step.ended_at - step.started_at).total_seconds() * 1000))
        elif duration_ms is not None:
            step.duration_ms = duration_ms
        step.success = success
        if error:
            step.error = error[:4000]
    if retry_count is not None:
        step.retry_count = retry_count
    if meta:
        step.meta = {**(step.meta or {}), **json_safe(meta)}
    step.save()

    structured_log(
        logging.INFO if success is not False else logging.ERROR,
        f"CommissioningStep:{name}",
        commissioning_run_id=str(run.id),
        success=success,
        duration_ms=step.duration_ms,
        error=error or None,
    )

    if success is False:
        capture_failure_snapshot(run, step_name=name, error=error)
        if run.status == CommissioningRunStatus.RUNNING:
            run.status = CommissioningRunStatus.FAILED
            run.completed_at = timezone.now()
            run.save(update_fields=["status", "completed_at"])

    return step


def begin_step(run: CommissioningRun | str, name: str, *, meta: dict | None = None) -> CommissioningRunStep:
    return mark_step(run, name, meta=meta, started_at=timezone.now())


def end_step(
    run: CommissioningRun | str,
    name: str,
    *,
    success: bool,
    error: str = "",
    retry_count: int = 0,
    meta: dict | None = None,
    duration_ms: int | None = None,
) -> CommissioningRunStep:
    return mark_step(
        run,
        name,
        success=success,
        error=error,
        retry_count=retry_count,
        meta=meta,
        duration_ms=duration_ms,
        ended_at=timezone.now(),
    )


def complete_run(run: CommissioningRun | str, *, success: bool = True, summary: dict | None = None) -> CommissioningRun:
    if isinstance(run, str):
        run = get_run(run)  # type: ignore[assignment]
    assert run is not None
    mark_step(run, STEP_RUN_COMPLETED, success=success)
    run.status = CommissioningRunStatus.COMPLETED if success else CommissioningRunStatus.FAILED
    run.completed_at = timezone.now()
    if summary:
        run.summary = {**(run.summary or {}), **json_safe(summary)}
    run.save(update_fields=["status", "completed_at", "summary"])
    return run


def capture_failure_snapshot(
    run: CommissioningRun | str,
    *,
    step_name: str = "",
    error: str = "",
) -> CommissioningFailureSnapshot:
    if isinstance(run, str):
        run_obj = get_run(run)
        if run_obj is None:
            raise ValueError(f"Unknown commissioning run: {run}")
        run = run_obj

    workspace = run.workspace
    ws = run.workstation or (workspace.workstation if workspace else None)
    cmd = None
    if ws:
        cmd = (
            RemoteCommand.objects.filter(workstation=ws)
            .order_by("-created_at")
            .values("id", "command_type", "status", "error_message", "result_message", "created_at")
            .first()
        )

    audits = []
    if workspace:
        audits = list(
            WorkspaceAudit.objects.filter(workspace=workspace)
            .order_by("-created_at")
            .values("action", "details", "success", "created_at")[:30]
        )

    events = []
    if ws:
        events = list(
            WorkstationEvent.objects.filter(workstation=ws)
            .order_by("-created_at")
            .values("category", "action", "details", "success", "created_at")[:30]
        )

    payload = {
        "commissioning_run_id": str(run.id),
        "step_name": step_name,
        "error": error,
        "captured_at": timezone.now().isoformat(),
        "correlation_id": get_correlation_id(),
        "workspace": {
            "id": str(workspace.id) if workspace else None,
            "status": getattr(workspace, "status", None),
            "sync_phase": getattr(workspace, "sync_phase", None),
            "sync_message": getattr(workspace, "sync_message", None),
            "booking_id": getattr(workspace, "booking_id", None),
        }
        if workspace
        else None,
        "workstation": {
            "id": str(ws.id) if ws else None,
            "agent_id": getattr(ws, "agent_id", None),
            "hostname": getattr(ws, "hostname", None),
            "status": getattr(ws, "status", None),
            "health_score": getattr(ws, "health_score", None),
            "last_heartbeat": ws.last_heartbeat.isoformat() if ws and ws.last_heartbeat else None,
        }
        if ws
        else None,
        "current_command": json_safe(cmd),
        "recent_workspace_audits": json_safe(audits),
        "recent_workstation_events": json_safe(events),
        "database_identifiers": {
            "run_id": str(run.id),
            "workspace_id": str(workspace.id) if workspace else None,
            "workstation_id": str(ws.id) if ws else None,
            "booking_id": run.booking_id,
        },
    }
    snap = CommissioningFailureSnapshot.objects.create(
        run=run,
        step_name=step_name or "",
        payload=payload,
    )
    structured_log(
        logging.ERROR,
        "CommissioningFailureSnapshot",
        commissioning_run_id=str(run.id),
        snapshot_id=str(snap.id),
        step_name=step_name,
    )
    return snap


def timeline_payload(run: CommissioningRun) -> dict[str, Any]:
    steps = []
    for s in run.steps.all():
        steps.append(
            {
                "name": s.name,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "duration_ms": s.duration_ms,
                "success": s.success,
                "retry_count": s.retry_count,
                "error": s.error or None,
                "meta": s.meta or {},
            }
        )
    return {
        "commissioning_run_id": str(run.id),
        "status": run.status,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "steps": steps,
    }


def summary_payload(run: CommissioningRun) -> dict[str, Any]:
    return {
        "commissioning_run_id": str(run.id),
        "status": run.status,
        "operator": getattr(run.operator, "email", None),
        "workstation_id": str(run.workstation_id) if run.workstation_id else None,
        "workspace_id": str(run.workspace_id) if run.workspace_id else None,
        "booking_id": run.booking_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "summary": run.summary or {},
        "failure_snapshots": [
            {"id": str(s.id), "step_name": s.step_name, "captured_at": s.captured_at.isoformat()}
            for s in run.failure_snapshots.all()
        ],
        "performance": [
            {
                "name": s.name,
                "duration_ms": s.duration_ms,
                "success": s.success,
                "retry_count": s.retry_count,
            }
            for s in run.steps.all()
        ],
    }


def _portal_logs_for_run(run: CommissioningRun) -> list[dict]:
    rid = str(run.id)
    since = run.started_at - timedelta(minutes=1) if run.started_at else timezone.now() - timedelta(hours=24)
    rows: list[dict] = []
    for a in WorkspaceAudit.objects.filter(created_at__gte=since).order_by("-created_at")[:200]:
        details = a.details or ""
        if rid in details or (run.workspace_id and a.workspace_id == run.workspace_id):
            rows.append(
                {
                    "source": "workspace_audit",
                    "created_at": a.created_at.isoformat(),
                    "action": a.action,
                    "details": details[:1000],
                    "success": a.success,
                }
            )
    if run.workstation_id:
        for e in WorkstationEvent.objects.filter(workstation_id=run.workstation_id, created_at__gte=since).order_by(
            "-created_at"
        )[:100]:
            rows.append(
                {
                    "source": "workstation_event",
                    "created_at": e.created_at.isoformat(),
                    "category": e.category,
                    "action": e.action,
                    "details": (e.details or "")[:1000],
                    "success": e.success,
                    "correlation_id": e.correlation_id,
                }
            )
    return rows


def _workspace_metadata(run: CommissioningRun) -> dict[str, Any]:
    if not run.workspace_id:
        return {}
    ws = run.workspace
    files = list(
        WorkspaceFile.objects.filter(workspace=ws, deleted=False)
        .values("id", "relative_path", "sha256", "size", "source")[:100]
    )
    return {
        "workspace_id": str(ws.id),
        "status": ws.status,
        "sync_phase": ws.sync_phase,
        "sync_message": ws.sync_message,
        "booking_id": ws.booking_id,
        "files": json_safe(files),
    }


def _checksum_results(run: CommissioningRun) -> dict[str, Any]:
    if not run.workspace_id:
        return {"files": []}
    files = list(
        WorkspaceFile.objects.filter(workspace_id=run.workspace_id, deleted=False).values(
            "relative_path", "sha256", "size"
        )[:100]
    )
    return {"files": json_safe(files), "note": "Portal-stored SHA-256; agent-side match verified during live run."}


def build_evidence_bundle_bytes(run: CommissioningRun, *, include_pdf: bool = True) -> bytes:
    """Build admin-only evidence ZIP in memory."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("commissioning_summary.json", json.dumps(summary_payload(run), indent=2, default=str))
        zf.writestr("execution_timeline.json", json.dumps(timeline_payload(run), indent=2, default=str))
        zf.writestr("portal_logs.json", json.dumps(_portal_logs_for_run(run), indent=2, default=str))
        zf.writestr(
            "agent_logs.json",
            json.dumps(
                {
                    "note": (
                        "Agent file logs are on the Analysis PC under "
                        r"C:\ProgramData\RemoteAnalysisAgent\Logs\. "
                        "Not pulled automatically; attach manually if collected."
                    ),
                    "workstation": run.workstation.hostname if run.workstation_id else None,
                },
                indent=2,
            ),
        )
        zf.writestr("workspace_metadata.json", json.dumps(_workspace_metadata(run), indent=2, default=str))
        zf.writestr(
            "api_summary.json",
            json.dumps(
                {
                    "commissioning_run_id": str(run.id),
                    "note": "Request/response bodies are not stored by default; see failure snapshots and audits.",
                    "failure_snapshots": [json_safe(s.payload) for s in run.failure_snapshots.all()],
                },
                indent=2,
                default=str,
            ),
        )
        zf.writestr("checksum_results.json", json.dumps(_checksum_results(run), indent=2, default=str))
        zf.writestr(
            "performance_metrics.json",
            json.dumps(summary_payload(run).get("performance") or [], indent=2, default=str),
        )
        if include_pdf:
            try:
                from iic_booking.remote_analysis.operations.toolkit import (
                    build_commissioning_report_payload,
                    render_commissioning_report_pdf,
                )

                payload = build_commissioning_report_payload(
                    actor=run.operator,
                    self_test={"commissioning_run_id": str(run.id), "timeline": timeline_payload(run)},
                )
                zf.writestr("commissioning_report.pdf", render_commissioning_report_pdf(payload))
            except Exception as exc:  # noqa: BLE001
                zf.writestr("commissioning_report_error.txt", str(exc))
    return buf.getvalue()


def persist_evidence_bundle(run: CommissioningRun) -> str:
    data = build_evidence_bundle_bytes(run)
    rel = f"remote_analysis/commissioning_runs/{run.id}/evidence.zip"
    if default_storage.exists(rel):
        default_storage.delete(rel)
    path = default_storage.save(rel, io.BytesIO(data))
    run.evidence_path = path
    run.save(update_fields=["evidence_path"])
    return path


def annotate_details(details: str) -> str:
    """Prefix audit/log details with Run ID when a run is in context (no duplicate rows)."""
    rid = get_commissioning_run_id()
    if not rid:
        return details
    tag = f"[commissioning_run={rid}]"
    if tag in (details or ""):
        return details
    if details:
        return f"{tag} {details}"
    return tag


def active_run_for_workspace(workspace: AnalysisWorkspace | None) -> CommissioningRun | None:
    if workspace is None:
        return None
    return (
        CommissioningRun.objects.filter(workspace_id=workspace.id, status=CommissioningRunStatus.RUNNING)
        .order_by("-started_at")
        .first()
    )


def active_run_id_for_workstation(workstation) -> str | None:
    if workstation is None:
        return None
    run = (
        CommissioningRun.objects.filter(workstation_id=workstation.pk, status=CommissioningRunStatus.RUNNING)
        .order_by("-started_at")
        .only("id")
        .first()
    )
    return str(run.id) if run else None


_FAILED_PHASES = frozenset(
    {
        "PreparationFailed",
        "UploadFailed",
        "CleanupFailed",
        "Cancelled",
    }
)


def observe_sync_phase(workspace: AnalysisWorkspace, phase: str, *, message: str = "") -> None:
    """
    Map workspace lifecycle phase changes onto commissioning timeline steps.
    No-op unless a RUNNING CommissioningRun is linked to the workspace.
    """
    run = active_run_for_workspace(workspace)
    if run is None:
        return

    failed = phase in _FAILED_PHASES
    with bind_run_context(run):
        if phase == "DownloadingInput":
            begin_step(run, STEP_AGENT_DOWNLOAD_STARTED, meta={"phase": phase})
        elif phase == "VerifyingInput":
            end_step(run, STEP_AGENT_DOWNLOAD_STARTED, success=True)
            end_step(run, STEP_AGENT_DOWNLOAD_FINISHED, success=True)
            begin_step(run, STEP_INPUT_VERIFICATION, meta={"phase": phase})
        elif phase == "InputReady":
            end_step(run, STEP_INPUT_VERIFICATION, success=True, meta={"phase": phase})
        elif phase == "SessionStarting":
            begin_step(run, STEP_ANALYSIS_STARTED, meta={"phase": phase})
        elif phase == "SessionActive":
            # ensure started even if SessionStarting was skipped
            begin_step(run, STEP_ANALYSIS_STARTED, meta={"phase": phase})
        elif phase == "CollectingOutput":
            end_step(run, STEP_ANALYSIS_STARTED, success=True)
            end_step(run, STEP_ANALYSIS_FINISHED, success=True)
            begin_step(run, STEP_OUTPUT_COLLECTION_STARTED, meta={"phase": phase})
        elif phase == "UploadingOutput":
            begin_step(run, STEP_OUTPUT_COLLECTION_STARTED, meta={"phase": phase})
        elif phase == "UploadVerified":
            end_step(run, STEP_OUTPUT_COLLECTION_STARTED, success=True)
            end_step(run, STEP_OUTPUT_COLLECTION_FINISHED, success=True)
            end_step(run, STEP_CHECKSUM_VERIFICATION, success=True, meta={"phase": phase})
        elif phase == "Cleanup":
            begin_step(run, STEP_CLEANUP_STARTED, meta={"phase": phase})
        elif phase == "Completed":
            end_step(run, STEP_CLEANUP_STARTED, success=True)
            end_step(run, STEP_CLEANUP_FINISHED, success=True)
            complete_run(run, success=True, summary={"final_phase": phase})
            try:
                persist_evidence_bundle(run)
            except Exception:  # noqa: BLE001
                logger.exception("Evidence bundle failed for run %s", run.id)
        elif failed:
            err = message or f"phase={phase}"
            # Close open-ish steps as failed without inventing new names
            for name in (
                STEP_AGENT_DOWNLOAD_STARTED,
                STEP_INPUT_VERIFICATION,
                STEP_ANALYSIS_STARTED,
                STEP_OUTPUT_COLLECTION_STARTED,
                STEP_CLEANUP_STARTED,
            ):
                step = CommissioningRunStep.objects.filter(run=run, name=name, success__isnull=True).first()
                if step and step.started_at and not step.ended_at:
                    end_step(run, name, success=False, error=err)
                    break
            else:
                capture_failure_snapshot(run, step_name=phase, error=err)
                if run.status == CommissioningRunStatus.RUNNING:
                    run.status = CommissioningRunStatus.FAILED
                    run.completed_at = timezone.now()
                    run.save(update_fields=["status", "completed_at"])
            try:
                persist_evidence_bundle(run)
            except Exception:  # noqa: BLE001
                logger.exception("Evidence bundle failed for run %s", run.id)
