"""
Admin-only Remote Analysis commissioning console.

Drives Portal → Agent → Portal file sync without Guacamole / analysis software.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from django.conf import settings as django_settings
from django.contrib.auth import login as django_login
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.middleware.csrf import get_token as get_csrf_token
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.html import escape
from rest_framework import authentication, exceptions, status
from rest_framework.authentication import SessionAuthentication
from rest_framework.authtoken.models import Token
from rest_framework.decorators import api_view, authentication_classes, parser_classes, permission_classes
from rest_framework.exceptions import NotAuthenticated, PermissionDenied
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from iic_booking.remote_analysis.constants import (
    AGENT_INPUT_PORTAL_FOLDERS,
    CommandType,
    WorkstationStatus,
    WorkspaceAuditAction,
    WorkspaceStatus,
    WorkspaceSyncPhase,
    normalize_sync_phase,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.workspace.audit import audit_workspace
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceAudit, WorkspaceFile
from iic_booking.users.api.token_auth import TokenAuthenticationWithInactivity

logger = logging.getLogger(__name__)

_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]

# Named commissioning lifecycle events (portal audit trail).
EVT_WORKSPACE_CREATED = "WorkspaceCreated"
EVT_COMMAND_QUEUED = "CommandQueued"
EVT_INPUT_DOWNLOADING = "InputDownloading"
EVT_INPUT_READY = "InputReady"
EVT_COLLECT_REQUESTED = "CollectRequested"
EVT_UPLOAD_VERIFIED = "UploadVerified"
EVT_COMPLETED = "Completed"
EVT_CLEANUP_STARTED = "CleanupStarted"
EVT_CLEANUP_FINISHED = "CleanupFinished"
EVT_PAUSE_WAITING = "WaitingForAnalysis"


def _commissioning_event(workspace: AnalysisWorkspace | None, event: str, *, details: str = "", actor=None, success: bool = True) -> None:
    from iic_booking.remote_analysis.operations.commissioning_observability import annotate_details, get_commissioning_run_id

    details = annotate_details(details)
    audit_workspace(
        workspace,
        WorkspaceAuditAction.SYNC,
        details=f"Commissioning:{event}" + (f" | {details}" if details else ""),
        actor=actor,
        success=success,
    )
    logger.info(
        "Commissioning %s | run=%s | workspace=%s | %s",
        event,
        get_commissioning_run_id(),
        getattr(workspace, "id", None),
        details,
    )


def build_commissioning_payload(*, workspace_id: str | None = None, booking_id: str | None = None) -> dict[str, Any]:
    """Aggregate status for the commissioning console (polled every 5s)."""
    from iic_booking.equipment.models import Booking, BookingStatus

    workstations = []
    for ws in AnalysisWorkstation.objects.filter(enabled=True).order_by("hostname")[:100]:
        age = None
        if ws.last_heartbeat:
            age = int((timezone.now() - ws.last_heartbeat).total_seconds())
        workstations.append(
            {
                "id": str(ws.id),
                "agent_id": ws.agent_id,
                "hostname": ws.hostname,
                "display_name": ws.display_name,
                "status": ws.status,
                "health_score": ws.health_score,
                "current_command": ws.current_command or "",
                "heartbeat_age_seconds": age,
                "last_heartbeat": ws.last_heartbeat.isoformat() if ws.last_heartbeat else None,
            }
        )

    bookings = []
    qs = (
        Booking.objects.filter(
            status=BookingStatus.COMPLETED,
            equipment__enable_remote_analysis=True,
        )
        .select_related("equipment", "user")
        .order_by("-booking_id")[:50]
    )
    for b in qs:
        bookings.append(
            {
                "booking_id": b.booking_id,
                "virtual_booking_id": b.virtual_booking_id or "",
                "equipment": getattr(b.equipment, "name", str(b.equipment_id)),
                "user": getattr(b.user, "email", str(b.user_id)),
                "analysis_available": bool(b.analysis_available),
                "reservation_id": str(b.analysis_reservation_id) if b.analysis_reservation_id else None,
                "workspace_id": str(b.analysis_workspace_id) if b.analysis_workspace_id else None,
            }
        )

    workspaces = []
    for w in (
        AnalysisWorkspace.objects.select_related("workstation", "booking", "reservation", "user")
        .exclude(status=WorkspaceStatus.DELETED)
        .order_by("-updated_at")[:40]
    ):
        workspaces.append(_serialize_workspace_detail(w))

    selected = None
    if workspace_id:
        w = AnalysisWorkspace.objects.filter(pk=workspace_id).select_related("workstation", "booking").first()
        if w:
            selected = _serialize_workspace_detail(w, full=True)
    elif booking_id:
        try:
            bid = int(booking_id)
        except (TypeError, ValueError):
            bid = None
        if bid is not None:
            w = (
                AnalysisWorkspace.objects.filter(booking_id=bid)
                .exclude(status=WorkspaceStatus.DELETED)
                .order_by("-updated_at")
                .first()
            )
            if w:
                selected = _serialize_workspace_detail(w, full=True)

    return {
        "generated_at": timezone.now().isoformat(),
        "poll_interval_seconds": 5,
        "workstations": workstations,
        "bookings": bookings,
        "workspaces": workspaces,
        "selected": selected,
        "operator_notes": {
            "pause_after": "InputReady — copy dummy result file(s) into agent Output/ then Collect",
            "agent_session_root": r"C:\ProgramData\RemoteAnalysisAgent\Sessions\{reservation_id}",
            "agent_log": r"C:\ProgramData\RemoteAnalysisAgent\Logs\raa-YYYYMMDD.log",
            "no_guacamole": True,
        },
    }


def _serialize_workspace_detail(workspace: AnalysisWorkspace, *, full: bool = False) -> dict[str, Any]:
    phase = normalize_sync_phase(workspace.sync_phase) or workspace.sync_phase or ""

    if full:
        input_files = []
        for folder in AGENT_INPUT_PORTAL_FOLDERS:
            input_files.extend(
                list(
                    WorkspaceFile.objects.filter(
                        workspace=workspace,
                        deleted=False,
                        is_current=True,
                        relative_path__startswith=f"{folder}/",
                    ).values("id", "relative_path", "sha256", "size", "source")[:50]
                )
            )
        output_files = list(
            WorkspaceFile.objects.filter(
                workspace=workspace,
                deleted=False,
                is_current=True,
                relative_path__startswith="Processed/",
            ).values("id", "relative_path", "sha256", "size", "source")[:100]
        )
        input_file_count = len(input_files)
        output_file_count = len(output_files)
    else:
        input_files = []
        output_files = []
        input_file_count = 0
        for folder in AGENT_INPUT_PORTAL_FOLDERS:
            input_file_count += WorkspaceFile.objects.filter(
                workspace=workspace,
                deleted=False,
                is_current=True,
                relative_path__startswith=f"{folder}/",
            ).count()
        output_file_count = WorkspaceFile.objects.filter(
            workspace=workspace,
            deleted=False,
            is_current=True,
            relative_path__startswith="Processed/",
        ).count()

    payload = {
        "id": str(workspace.id),
        "status": workspace.status,
        "sync_phase": phase,
        "sync_progress_percent": workspace.sync_progress_percent,
        "sync_message": workspace.sync_message or "",
        "last_synced_at": workspace.last_synced_at.isoformat() if workspace.last_synced_at else None,
        "upload_verified_at": workspace.upload_verified_at.isoformat() if workspace.upload_verified_at else None,
        "reservation_id": str(workspace.reservation_id) if workspace.reservation_id else None,
        "booking_id": workspace.booking_id,
        "workstation_id": str(workspace.workstation_id) if workspace.workstation_id else None,
        "workstation_status": workspace.workstation.status if workspace.workstation_id else None,
        "workstation_hostname": workspace.workstation.hostname if workspace.workstation_id else None,
        "local_agent_path": workspace.local_agent_path or "",
        "input_file_count": input_file_count,
        "output_file_count": output_file_count,
        "is_input_ready": WorkspaceSyncService().is_input_ready(workspace),
        "lifecycle_hint": _lifecycle_hint(phase),
    }
    if not full:
        return payload

    transfers = []
    for t in workspace.transfers.order_by("-created_at")[:20]:
        transfers.append(
            {
                "id": str(t.id),
                "direction": t.direction,
                "status": t.status,
                "retry_count": t.retry_count,
                "checksum_expected": t.checksum_expected or "",
                "checksum_actual": t.checksum_actual or "",
                "error_message": t.error_message or "",
                "started_at": t.started_at.isoformat() if t.started_at else None,
                "completed_at": t.completed_at.isoformat() if t.completed_at else None,
            }
        )

    commands = []
    if workspace.workstation_id:
        for cmd in RemoteCommand.objects.filter(workstation_id=workspace.workstation_id).order_by("-created_at")[:25]:
            commands.append(
                {
                    "id": str(cmd.id),
                    "type": cmd.command_type,
                    "status": cmd.status,
                    "result_message": (cmd.result_message or "")[:300],
                    "error_message": (cmd.error_message or "")[:300],
                    "created_at": cmd.created_at.isoformat() if cmd.created_at else None,
                    "delivered_at": cmd.delivered_at.isoformat() if cmd.delivered_at else None,
                    "completed_at": cmd.completed_at.isoformat() if cmd.completed_at else None,
                }
            )

    events = []
    for row in WorkspaceAudit.objects.filter(workspace=workspace).order_by("-created_at")[:40]:
        events.append(
            {
                "action": row.action,
                "details": row.details or "",
                "success": row.success,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )

    payload.update(
        {
            "input_files": [
                {
                    "id": str(f["id"]),
                    "relative_path": f["relative_path"],
                    "sha256": f["sha256"] or "",
                    "size": f["size"],
                    "source": f["source"],
                }
                for f in input_files
            ],
            "output_files": [
                {
                    "id": str(f["id"]),
                    "relative_path": f["relative_path"],
                    "sha256": f["sha256"] or "",
                    "size": f["size"],
                    "source": f["source"],
                }
                for f in output_files
            ],
            "transfers": transfers,
            "commands": commands,
            "events": events,
            "agent_session_hint": (
                rf"C:\ProgramData\RemoteAnalysisAgent\Sessions\{workspace.reservation_id}"
                if workspace.reservation_id
                else ""
            ),
        }
    )
    return payload


def _lifecycle_hint(phase: str) -> str:
    """Read-only label for UI (no DB writes on GET)."""
    mapping = {
        WorkspaceSyncPhase.DOWNLOADING_INPUT: EVT_INPUT_DOWNLOADING,
        WorkspaceSyncPhase.VERIFYING_INPUT: EVT_INPUT_DOWNLOADING,
        WorkspaceSyncPhase.INPUT_READY: EVT_INPUT_READY,
        WorkspaceSyncPhase.COLLECTING_OUTPUT: EVT_COLLECT_REQUESTED,
        WorkspaceSyncPhase.UPLOADING_OUTPUT: EVT_COLLECT_REQUESTED,
        WorkspaceSyncPhase.UPLOAD_VERIFIED: EVT_UPLOAD_VERIFIED,
        WorkspaceSyncPhase.CLEANUP: EVT_CLEANUP_STARTED,
        WorkspaceSyncPhase.COMPLETED: EVT_COMPLETED,
    }
    return mapping.get(phase, phase or "")


def create_workspace(*, booking_id: int, workstation_id: str, actor=None, ingest: bool = False) -> AnalysisWorkspace:
    """Create/ensure reservation + workspace for a completed booking and bind a workstation."""
    from iic_booking.equipment.models import Booking
    from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService

    booking = get_object_or_404(Booking, pk=booking_id)
    workstation = get_object_or_404(AnalysisWorkstation, pk=workstation_id)

    svc = BookingRemoteAnalysisService()
    try:
        reservation = svc.ensure_reservation(booking, actor=actor, auto_allocate=False)
    except ValueError as exc:
        # Commissioning override: create reservation when eligibility blocks (logged).
        logger.warning(
            "Commissioning create_workspace eligibility override booking=%s: %s",
            booking_id,
            exc,
        )
        from iic_booking.remote_analysis.services.reservation import ReservationService

        start = timezone.now()
        end = start + timedelta(hours=4)
        reservation = ReservationService().create_reservation(
            user=booking.user,
            requested_start=start,
            requested_end=end,
            booking=booking,
            created_by=actor,
            auto_allocate=False,
        )
        booking.analysis_reservation = reservation
        booking.save(update_fields=["analysis_reservation", "updated_at"])

    if reservation.workstation_id is None:
        reservation.workstation = workstation
        reservation.save(update_fields=["workstation", "updated_at"])

    sync = WorkspaceSyncService()
    workspace = sync.ensure_for_reservation(reservation, actor=actor, ingest=ingest)
    if workspace.workstation_id != workstation.id:
        workspace.workstation = workstation
        workspace.save(update_fields=["workstation", "updated_at"])
    if booking.analysis_workspace_id != workspace.id:
        booking.analysis_workspace = workspace
        booking.save(update_fields=["analysis_workspace", "updated_at"])

    _commissioning_event(
        workspace,
        EVT_WORKSPACE_CREATED,
        details=f"booking={booking_id} workstation={workstation.hostname}",
        actor=actor,
    )
    return workspace


def prepare_workspace(*, workspace_id: str, actor=None) -> RemoteCommand:
    """Issue PREPARE_WORKSTATION (layout + input download). No Guacamole."""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not workspace.workstation_id:
        raise ValueError("Workspace has no workstation assigned")

    sync = WorkspaceSyncService()
    sync.set_sync_phase(
        workspace,
        WorkspaceSyncPhase.DOWNLOADING_INPUT,
        percent=25,
        message="Commissioning: downloading input to workstation",
        actor=actor,
    )
    _commissioning_event(workspace, EVT_INPUT_DOWNLOADING, actor=actor)

    ws = workspace.workstation
    if ws.status not in {WorkstationStatus.DISABLED, WorkstationStatus.MAINTENANCE}:
        ws.status = WorkstationStatus.PREPARING
        ws.save(update_fields=["status", "updated_at"])

    payload = sync.prepare_payload(workspace, session_id=str(workspace.reservation_id))
    cmd = CommandService().create_command(
        ws,
        CommandType.PREPARE_WORKSTATION,
        payload=payload,
        created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
    )
    _commissioning_event(
        workspace,
        EVT_COMMAND_QUEUED,
        details=f"PREPARE_WORKSTATION command={cmd.id}",
        actor=actor,
    )
    return cmd


def collect_workspace(*, workspace_id: str, actor=None) -> Any:
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    cmd = WorkspaceSyncService().issue_collect_command(workspace, actor=actor)
    if cmd is None:
        raise ValueError("Collect failed — workspace has no workstation")
    _commissioning_event(
        workspace,
        EVT_COLLECT_REQUESTED,
        details=f"COLLECT_WORKSPACE command={cmd.id}",
        actor=actor,
    )
    return cmd


def cleanup_workspace(*, workspace_id: str, actor=None) -> RemoteCommand | None:
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    if not workspace.workstation_id:
        raise ValueError("Workspace has no workstation assigned")

    sync = WorkspaceSyncService()
    sync.set_sync_phase(
        workspace,
        WorkspaceSyncPhase.CLEANUP,
        percent=95,
        message="Commissioning: cleanup started",
        actor=actor,
    )
    _commissioning_event(workspace, EVT_CLEANUP_STARTED, actor=actor)

    cmd = CommandService().create_command(
        workspace.workstation,
        CommandType.CLEAN_WORKSTATION,
        payload={
            "session_id": str(workspace.reservation_id),
            "workspace_id": str(workspace.id),
            "local_path": workspace.local_agent_path,
            "reason": "commissioning_cleanup",
            "defer_output_cleanup": False,
            "delete_folders": ["Input", "Working", "Output", "Logs", "Temp"],
        },
        created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
    )
    _commissioning_event(
        workspace,
        EVT_COMMAND_QUEUED,
        details=f"CLEAN_WORKSTATION command={cmd.id}",
        actor=actor,
    )

    ws = workspace.workstation
    if ws.status not in {WorkstationStatus.DISABLED, WorkstationStatus.MAINTENANCE}:
        # Agent will flip to AVAILABLE after CLEAN; portal marks AVAILABLE for commissioning visibility.
        ws.status = WorkstationStatus.CLEANING
        ws.save(update_fields=["status", "updated_at"])

    return cmd


def mark_pause_waiting(*, workspace_id: str, actor=None) -> None:
    """Record operator pause after InputReady (manual Output drop)."""
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    _commissioning_event(workspace, EVT_PAUSE_WAITING, details="Operator will place dummy Output files", actor=actor)


def upload_sample_input(*, workspace_id: str, uploaded_file, actor=None, folder: str = "RawData") -> WorkspaceFile:
    workspace = get_object_or_404(AnalysisWorkspace, pk=workspace_id)
    row = TransferManager().upload(
        workspace,
        uploaded_file,
        folder=folder or "RawData",
        actor=actor,
        override_quota=True,
    )
    _commissioning_event(
        workspace,
        "SampleInputUploaded",
        details=f"{row.relative_path} sha256={row.sha256}",
        actor=actor,
    )
    return row


def annotate_phase_milestones(workspace: AnalysisWorkspace) -> None:
    """
    Emit named audit events when phases are reached.

    Intended for explicit action paths — do not call from GET polling
    (avoids write amplification every 5 seconds).
    """
    phase = normalize_sync_phase(workspace.sync_phase)
    recent = list(
        WorkspaceAudit.objects.filter(workspace=workspace, action=WorkspaceAuditAction.SYNC)
        .order_by("-created_at")
        .values_list("details", flat=True)[:30]
    )
    recent_text = " ".join(recent)

    def _seen(token: str) -> bool:
        return f"Commissioning:{token}" in recent_text

    if phase == WorkspaceSyncPhase.INPUT_READY and not _seen(EVT_INPUT_READY):
        _commissioning_event(workspace, EVT_INPUT_READY, details=workspace.sync_message or "")
        _commissioning_event(workspace, EVT_PAUSE_WAITING, details="Ready for manual Output placement")
    if phase == WorkspaceSyncPhase.UPLOAD_VERIFIED and not _seen(EVT_UPLOAD_VERIFIED):
        _commissioning_event(workspace, EVT_UPLOAD_VERIFIED, details=workspace.sync_message or "")
    if phase == WorkspaceSyncPhase.COMPLETED and not _seen(EVT_COMPLETED):
        _commissioning_event(workspace, EVT_COMPLETED, details=workspace.sync_message or "")
        _commissioning_event(workspace, EVT_CLEANUP_FINISHED, details="Pipeline complete")


# ---------------------------------------------------------------------------
# HTTP views
# ---------------------------------------------------------------------------


def wants_interactive_html(request) -> bool:
    """True for browser HTML console requests (not JSON API clients)."""
    if (request.query_params.get("view") or request.query_params.get("render") or "").lower() == "html":
        return True
    accept = (request.META.get("HTTP_ACCEPT") or "").strip().lower()
    if not accept:
        return False
    parts = [p.split(";")[0].strip() for p in accept.split(",") if p.strip()]
    if "application/json" in parts:
        return False
    return "text/html" in parts or "application/xhtml+xml" in parts


class QueryParamTokenAuthentication(authentication.BaseAuthentication):
    """
    One-shot browser handoff for portal UI: authenticate GET HTML via ``?token=``.

    JSON/API requests never authenticate from the query string (header Token or
    session only), so API auth is not weakened.
    """

    def authenticate(self, request):
        if getattr(request, "method", "GET").upper() != "GET":
            return None
        if not wants_interactive_html(request):
            return None
        key = (request.query_params.get("token") or "").strip()
        if not key:
            return None
        try:
            token = Token.objects.select_related("user").get(key=key)
        except Token.DoesNotExist as exc:
            raise exceptions.AuthenticationFailed("Invalid token.") from exc
        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("User inactive or deleted.")
        return (token.user, token)


# Token header first so API clients with a leftover session cookie are not forced through CSRF.
_BROWSER_AUTH = [
    TokenAuthenticationWithInactivity,
    SessionAuthentication,
    QueryParamTokenAuthentication,
]


def portal_login_redirect_url(request) -> str:
    """Send anonymous browser users to the portal login, then back here."""
    next_url = request.build_absolute_uri()
    # Never echo a raw token back through the login next URL.
    parts = urlsplit(next_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "token"]
    next_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))

    frontend = (getattr(django_settings, "FRONTEND_URL", "") or "").rstrip("/")
    if frontend:
        return f"{frontend}/login?{urlencode({'next': next_url})}"
    try:
        login_path = reverse(django_settings.LOGIN_URL)
    except Exception:  # noqa: BLE001
        login_path = "/accounts/login/"
    return f"{login_path}?{urlencode({'next': request.get_full_path()})}"


def _strip_token_and_redirect(request) -> HttpResponseRedirect:
    """After query-token auth, persist Django session and drop token from the URL."""
    django_login(request, request.user, backend="django.contrib.auth.backends.ModelBackend")
    parts = urlsplit(request.build_absolute_uri())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() != "token"]
    if not any(k == "view" for k, _ in query):
        query.append(("view", "html"))
    target = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
    return HttpResponseRedirect(target)


def _error_hint(exc: BaseException) -> str:
    msg = str(exc)
    if "upload_verified_at" in msg or "does not exist" in msg.lower():
        return (
            "Database schema is behind the application code. "
            "Run: python manage.py migrate remote_analysis "
            "(requires migration 0010_workspace_lifecycle_phases)."
        )
    return "See server logs for the full traceback."


def _render_error_page(exc: BaseException, *, traceback_text: str = "") -> HttpResponse:
    hint = escape(_error_hint(exc))
    detail = escape(f"{type(exc).__name__}: {exc}")
    if traceback_text and django_settings.DEBUG:
        tb_block = f"<h2>Traceback</h2><pre>{escape(traceback_text)}</pre>"
    else:
        tb_block = (
            "<p>Full traceback has been written to server logs. "
            "Enable DEBUG to also show it on this page.</p>"
        )
    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/><title>Commissioning Error</title>
<style>
body{{font-family:Segoe UI,system-ui,sans-serif;margin:2rem;background:#111827;color:#f3f4f6}}
pre{{white-space:pre-wrap;background:#1f2937;padding:1rem;border-radius:8px;overflow:auto}}
.hint{{color:#fbbf24;margin:1rem 0}}
a{{color:#93c5fd}}
</style></head><body>
<h1>Sync Commissioning Console — Error</h1>
<p class="hint">{hint}</p>
<p><strong>{detail}</strong></p>
{tb_block}
<p><a href="/api/v1/analysis/operations/toolkit/?view=html">Commissioning Toolkit</a>
 · <a href="/api/v1/analysis/operations/diagnostics/?view=html">Deployment diagnostics</a></p>
</body></html>"""
    return HttpResponse(html, status=500, content_type="text/html; charset=utf-8")


class CommissioningConsoleView(APIView):
    """
    Admin commissioning console.

    Permissions unchanged: IsAuthenticated + CanManageRemoteAnalysis.
    Browser HTML (?view=html / Accept: text/html) redirects anonymous users to portal login.
    JSON/API clients still receive the standard DRF authentication error.
    """

    authentication_classes = _BROWSER_AUTH
    permission_classes = _MANAGE

    def handle_exception(self, exc):
        request = self.request
        if isinstance(exc, NotAuthenticated) and wants_interactive_html(request):
            return HttpResponseRedirect(portal_login_redirect_url(request))
        if isinstance(exc, PermissionDenied) and wants_interactive_html(request):
            return HttpResponse(
                "<h1>Forbidden</h1><p>You are signed in but lack Remote Analysis manage permission.</p>",
                status=403,
                content_type="text/html; charset=utf-8",
            )
        return super().handle_exception(exc)

    def get(self, request, *args, **kwargs):
        want_html = wants_interactive_html(request)
        # Portal UI may open this URL with ?token=<drf_token>&view=html once; convert to session.
        if want_html and request.query_params.get("token") and request.user.is_authenticated:
            return _strip_token_and_redirect(request)

        workspace_id = request.query_params.get("workspace_id") or None
        booking_id = request.query_params.get("booking_id") or None
        try:
            payload = build_commissioning_payload(workspace_id=workspace_id, booking_id=booking_id)
            if want_html or (request.query_params.get("view") or "").lower() == "html":
                get_csrf_token(request)  # ensure csrftoken cookie for SessionAuthentication POSTs
                return HttpResponse(render_commissioning_html(payload), content_type="text/html; charset=utf-8")
            return Response(payload)
        except Exception as exc:  # noqa: BLE001
            import traceback

            if isinstance(exc, Http404):
                raise

            tb = traceback.format_exc()
            logger.exception("Commissioning console failed")
            if want_html or (request.query_params.get("view") or "").lower() == "html":
                return _render_error_page(exc, traceback_text=tb)
            return Response(
                {
                    "detail": str(exc),
                    "error_type": type(exc).__name__,
                    "hint": _error_hint(exc),
                    "traceback": tb if django_settings.DEBUG else None,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


commissioning_console = CommissioningConsoleView.as_view()


@api_view(["POST"])
@authentication_classes(_BROWSER_AUTH)
@permission_classes(_MANAGE)
@parser_classes([JSONParser, MultiPartParser, FormParser])
def commissioning_action(request):
    """
    POST /api/v1/analysis/operations/commissioning/action/

    Body JSON: {\"action\": \"create|prepare|collect|cleanup|refresh|pause\", ...}
    Or multipart for action=upload with file field.

    Optional observability (does not change workflow):
    ``commissioning_run_id`` — attach timeline / audit correlation.
    """
    from iic_booking.remote_analysis.operations.commissioning_observability import (
        STEP_BOOKING_SELECTED,
        STEP_CLEANUP_FINISHED,
        STEP_CLEANUP_STARTED,
        STEP_INPUT_UPLOAD_FINISHED,
        STEP_INPUT_UPLOAD_STARTED,
        STEP_OUTPUT_COLLECTION_FINISHED,
        STEP_OUTPUT_COLLECTION_STARTED,
        STEP_WORKSPACE_CREATED,
        bind_run_context,
        begin_step,
        end_step,
        get_run,
        link_workspace,
    )

    action = (request.data.get("action") or "").strip().lower()
    actor = request.user
    run = get_run(str(request.data.get("commissioning_run_id") or "").strip() or "")

    def _attach(payload: dict) -> dict:
        if run:
            payload = {**payload, "commissioning_run_id": str(run.id)}
        return payload

    with bind_run_context(run):
        try:
            if action in {"create", "create_workspace"}:
                booking_id = int(request.data.get("booking_id"))
                workstation_id = str(request.data.get("workstation_id") or "")
                if not workstation_id:
                    return Response({"detail": "workstation_id required"}, status=status.HTTP_400_BAD_REQUEST)
                if run:
                    begin_step(run, STEP_BOOKING_SELECTED, meta={"booking_id": booking_id})
                    end_step(run, STEP_BOOKING_SELECTED, success=True)
                    begin_step(run, STEP_WORKSPACE_CREATED)
                ingest = str(request.data.get("ingest") or "").lower() in {"1", "true", "yes"}
                workspace = create_workspace(
                    booking_id=booking_id,
                    workstation_id=workstation_id,
                    actor=actor,
                    ingest=ingest,
                )
                annotate_phase_milestones(workspace)
                if run:
                    link_workspace(run, workspace, booking_id=booking_id)
                    end_step(run, STEP_WORKSPACE_CREATED, success=True, meta={"workspace_id": str(workspace.id)})
                return Response(
                    _attach({"ok": True, "workspace": _serialize_workspace_detail(workspace, full=True)}),
                    status=status.HTTP_201_CREATED,
                )

            if action in {"prepare", "prepare_workspace"}:
                workspace_id = str(request.data.get("workspace_id") or "")
                cmd = prepare_workspace(workspace_id=workspace_id, actor=actor)
                return Response(
                    _attach({"ok": True, "command_id": str(cmd.id), "command_type": cmd.command_type})
                )

            if action in {"collect", "collect_output", "collect_workspace"}:
                workspace_id = str(request.data.get("workspace_id") or "")
                if run:
                    begin_step(run, STEP_OUTPUT_COLLECTION_STARTED)
                cmd = collect_workspace(workspace_id=workspace_id, actor=actor)
                if run:
                    end_step(run, STEP_OUTPUT_COLLECTION_STARTED, success=True)
                    end_step(
                        run,
                        STEP_OUTPUT_COLLECTION_FINISHED,
                        success=True,
                        meta={"command_id": str(cmd.id)},
                    )
                return Response(
                    _attach({"ok": True, "command_id": str(cmd.id), "command_type": cmd.command_type})
                )

            if action in {"cleanup", "cleanup_workspace"}:
                workspace_id = str(request.data.get("workspace_id") or "")
                if run:
                    begin_step(run, STEP_CLEANUP_STARTED)
                cmd = cleanup_workspace(workspace_id=workspace_id, actor=actor)
                if run:
                    end_step(run, STEP_CLEANUP_STARTED, success=True)
                    end_step(
                        run,
                        STEP_CLEANUP_FINISHED,
                        success=True,
                        meta={"command_id": str(cmd.id) if cmd else None},
                    )
                return Response(
                    _attach(
                        {
                            "ok": True,
                            "command_id": str(cmd.id) if cmd else None,
                            "command_type": CommandType.CLEAN_WORKSTATION,
                        }
                    )
                )

            if action in {"pause", "waiting"}:
                mark_pause_waiting(workspace_id=str(request.data.get("workspace_id") or ""), actor=actor)
                return Response(_attach({"ok": True, "paused": True}))

            if action == "upload":
                workspace_id = str(request.data.get("workspace_id") or "")
                uploaded = request.FILES.get("file") or request.FILES.get("upload")
                if not uploaded:
                    return Response({"detail": "Missing file"}, status=status.HTTP_400_BAD_REQUEST)
                folder = request.data.get("folder") or "RawData"
                if run:
                    begin_step(run, STEP_INPUT_UPLOAD_STARTED)
                row = upload_sample_input(
                    workspace_id=workspace_id,
                    uploaded_file=uploaded,
                    actor=actor,
                    folder=folder,
                )
                if run:
                    end_step(run, STEP_INPUT_UPLOAD_STARTED, success=True)
                    end_step(
                        run,
                        STEP_INPUT_UPLOAD_FINISHED,
                        success=True,
                        meta={"sha256": row.sha256, "path": row.relative_path},
                    )
                return Response(
                    _attach(
                        {
                            "ok": True,
                            "file": {
                                "id": str(row.id),
                                "relative_path": row.relative_path,
                                "sha256": row.sha256,
                                "size": row.size,
                            },
                        }
                    ),
                    status=status.HTTP_201_CREATED,
                )

            if action == "refresh":
                workspace_id = request.data.get("workspace_id") or None
                return Response(_attach(build_commissioning_payload(workspace_id=workspace_id)))

            return Response({"detail": f"Unknown action: {action}"}, status=status.HTTP_400_BAD_REQUEST)
        except (ValueError, TransferError, TypeError) as exc:
            logger.warning("Commissioning action %s rejected: %s", action, exc)
            if run:
                from iic_booking.remote_analysis.operations.commissioning_observability import capture_failure_snapshot

                capture_failure_snapshot(run, step_name=action, error=str(exc))
            return Response(
                _attach({"detail": str(exc), "hint": _error_hint(exc)}),
                status=status.HTTP_400_BAD_REQUEST,
            )
        except Exception as exc:  # noqa: BLE001
            import traceback

            if isinstance(exc, Http404):
                raise

            tb = traceback.format_exc()
            logger.exception("Commissioning action %s failed", action)
            if run:
                from iic_booking.remote_analysis.operations.commissioning_observability import capture_failure_snapshot

                capture_failure_snapshot(run, step_name=action, error=str(exc))
            return Response(
                _attach(
                    {
                        "detail": str(exc),
                        "error_type": type(exc).__name__,
                        "hint": _error_hint(exc),
                        "traceback": tb if django_settings.DEBUG else None,
                    }
                ),
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )


def render_commissioning_html(payload: dict[str, Any]) -> str:
    data_json = json.dumps(payload, default=str).replace("</", "<\\/")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Remote Analysis — Sync Commissioning</title>
<style>
:root {{
  --bg:#0f1419; --panel:#1a222c; --line:#2c3845; --text:#e7eef5; --muted:#9aabbc;
  --accent:#3d8bfd; --ok:#3dd68c; --warn:#f5a524; --bad:#f31260;
  --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  --sans: "Segoe UI", system-ui, sans-serif;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:var(--sans); background:var(--bg); color:var(--text); }}
header {{ padding:20px 24px; border-bottom:1px solid var(--line); display:flex; gap:16px; align-items:center; justify-content:space-between; flex-wrap:wrap; }}
h1 {{ margin:0; font-size:20px; font-weight:600; }}
.badge {{ font-size:12px; padding:4px 8px; border:1px solid var(--line); border-radius:999px; color:var(--muted); }}
main {{ display:grid; grid-template-columns: 320px 1fr; gap:16px; padding:16px 24px 40px; }}
@media (max-width: 980px) {{ main {{ grid-template-columns:1fr; }} }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px; }}
.card h2 {{ margin:0 0 10px; font-size:14px; color:var(--muted); font-weight:600; letter-spacing:.04em; text-transform:uppercase; }}
label {{ display:block; font-size:12px; color:var(--muted); margin:8px 0 4px; }}
select, input[type=file], button, input[type=text] {{
  width:100%; padding:8px 10px; border-radius:8px; border:1px solid var(--line);
  background:#121820; color:var(--text); font:inherit;
}}
button {{ cursor:pointer; background:var(--accent); border-color:transparent; font-weight:600; margin-top:8px; }}
button.secondary {{ background:#243041; }}
button.danger {{ background:var(--bad); }}
button:disabled {{ opacity:.45; cursor:not-allowed; }}
.row {{ display:flex; gap:8px; flex-wrap:wrap; }}
.row button {{ width:auto; flex:1 1 140px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th, td {{ text-align:left; padding:8px 6px; border-bottom:1px solid var(--line); vertical-align:top; }}
th {{ color:var(--muted); font-weight:600; }}
.mono {{ font-family:var(--mono); font-size:12px; }}
.pill {{ display:inline-block; padding:2px 8px; border-radius:999px; font-size:11px; border:1px solid var(--line); }}
.pill.ok {{ color:var(--ok); border-color:var(--ok); }}
.pill.warn {{ color:var(--warn); border-color:var(--warn); }}
.pill.bad {{ color:var(--bad); border-color:var(--bad); }}
#flash {{ min-height:20px; font-size:13px; color:var(--muted); margin-top:8px; }}
#flash.err {{ color:var(--bad); }}
#flash.ok {{ color:var(--ok); }}
.hint {{ font-size:12px; color:var(--muted); line-height:1.45; margin-top:10px; }}
pre.log {{ max-height:220px; overflow:auto; background:#121820; padding:10px; border-radius:8px; font-size:11px; white-space:pre-wrap; }}
.statgrid {{ display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:8px; margin-bottom:12px; }}
.stat {{ background:#121820; border:1px solid var(--line); border-radius:8px; padding:10px; }}
.stat .v {{ font-size:18px; font-weight:600; }}
.stat .l {{ font-size:11px; color:var(--muted); margin-top:2px; }}
</style>
</head>
<body>
<header>
  <div>
    <h1>Sync Commissioning Console</h1>
    <div class="badge">No Guacamole · Portal ↔ Agent file pipeline</div>
  </div>
  <div class="badge" id="clock">—</div>
</header>
<main>
  <aside class="card">
    <h2>Setup</h2>
    <label>Completed booking</label>
    <select id="booking"></select>
    <label>Workstation</label>
    <select id="workstation"></select>
    <div class="row">
      <button id="btnCreate" type="button">Create Workspace</button>
    </div>
    <label>Sample input file → RawData</label>
    <input type="file" id="fileInput"/>
    <button id="btnUpload" class="secondary" type="button">Upload Input</button>
    <div class="hint">
      After <b>InputReady</b>, pause and copy a dummy result into the agent
      <span class="mono">Sessions\\{{reservation}}\\Output\\</span> folder, then Collect.
    </div>
    <div id="flash"></div>
  </aside>
  <section>
    <div class="card" style="margin-bottom:16px">
      <h2>Selected workspace</h2>
      <div class="statgrid" id="stats"></div>
      <div class="row">
        <button id="btnPrepare" type="button">Prepare Workspace</button>
        <button id="btnRefresh" class="secondary" type="button">Refresh Status</button>
        <button id="btnCollect" type="button">Collect Output</button>
        <button id="btnCleanup" class="danger" type="button">Cleanup Workspace</button>
        <button id="btnLogs" class="secondary" type="button">View Logs</button>
      </div>
      <div class="hint mono" id="pathHint"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Active workspaces</h2>
      <div style="overflow:auto"><table><thead><tr>
        <th>Workspace</th><th>Phase</th><th>Status</th><th>Workstation</th><th>In</th><th>Out</th>
      </tr></thead><tbody id="wsRows"></tbody></table></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Files / checksums</h2>
      <div style="overflow:auto"><table><thead><tr>
        <th>Role</th><th>Path</th><th>SHA-256</th><th>Size</th><th>Source</th>
      </tr></thead><tbody id="fileRows"></tbody></table></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <h2>Command queue</h2>
      <div style="overflow:auto"><table><thead><tr>
        <th>Type</th><th>Status</th><th>Result</th><th>Created</th><th>Completed</th>
      </tr></thead><tbody id="cmdRows"></tbody></table></div>
    </div>
    <div class="card">
      <h2>Lifecycle events</h2>
      <pre class="log mono" id="eventLog"></pre>
      <div class="hint">Transfers / retries</div>
      <pre class="log mono" id="xferLog"></pre>
    </div>
  </section>
</main>
<script>
const API_BASE = "/api/v1/analysis/operations/commissioning/";
const ACTION_URL = "/api/v1/analysis/operations/commissioning/action/";
const INITIAL = {data_json};
let state = INITIAL;
let selectedId = (INITIAL.selected && INITIAL.selected.id) || null;
const csrftoken = (document.cookie.match(/csrftoken=([^;]+)/) || [])[1] || "";

function flash(msg, ok) {{
  const el = document.getElementById("flash");
  el.textContent = msg || "";
  el.className = ok === false ? "err" : (ok ? "ok" : "");
}}

function phasePill(phase) {{
  const p = (phase || "").toLowerCase();
  let cls = "pill";
  if (p.includes("ready") || p.includes("completed") || p.includes("verified")) cls += " ok";
  else if (p.includes("fail") || p.includes("cancel")) cls += " bad";
  else if (p) cls += " warn";
  return `<span class="${{cls}}">${{escapeHtml(phase || "—")}}</span>`;
}}

function escapeHtml(s) {{
  return String(s ?? "").replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;","'":"&#39;"}}[c]));
}}

function fillSelects() {{
  const b = document.getElementById("booking");
  const w = document.getElementById("workstation");
  const bVal = b.value;
  const wVal = w.value;
  b.innerHTML = state.bookings.map(x =>
    `<option value="${{x.booking_id}}">#${{x.booking_id}} ${{escapeHtml(x.equipment)}} (${{escapeHtml(x.user)}})</option>`
  ).join("") || `<option value="">No completed RA bookings</option>`;
  w.innerHTML = state.workstations.map(x =>
    `<option value="${{x.id}}">${{escapeHtml(x.hostname)}} · ${{escapeHtml(x.status)}} · health ${{x.health_score}}</option>`
  ).join("") || `<option value="">No workstations</option>`;
  if (bVal) b.value = bVal;
  if (wVal) w.value = wVal;
}}

function render() {{
  document.getElementById("clock").textContent = "Updated " + (state.generated_at || "");
  fillSelects();
  const rows = (state.workspaces || []).map(ws => {{
    const sel = ws.id === selectedId ? "font-weight:600" : "";
    return `<tr style="cursor:pointer;${{sel}}" data-id="${{ws.id}}">
      <td class="mono">${{escapeHtml(ws.id.slice(0,8))}}...</td>
      <td>${{phasePill(ws.sync_phase)}}</td>
      <td>${{escapeHtml(ws.status)}}</td>
      <td>${{escapeHtml(ws.workstation_hostname || "—")}} <span class="mono">${{escapeHtml(ws.workstation_status || "")}}</span></td>
      <td>${{ws.input_file_count}}</td>
      <td>${{ws.output_file_count}}</td>
    </tr>`;
  }}).join("") || `<tr><td colspan="6">No workspaces yet</td></tr>`;
  document.getElementById("wsRows").innerHTML = rows;
  document.querySelectorAll("#wsRows tr[data-id]").forEach(tr => {{
    tr.onclick = () => {{ selectedId = tr.getAttribute("data-id"); refresh(true); }};
  }});

  const sel = state.selected;
  const stats = document.getElementById("stats");
  if (!sel) {{
    stats.innerHTML = `<div class="stat"><div class="v">—</div><div class="l">Select or create a workspace</div></div>`;
    document.getElementById("fileRows").innerHTML = "";
    document.getElementById("cmdRows").innerHTML = "";
    document.getElementById("eventLog").textContent = "";
    document.getElementById("xferLog").textContent = "";
    document.getElementById("pathHint").textContent = "";
    return;
  }}
  stats.innerHTML = `
    <div class="stat"><div class="v">${{phasePill(sel.sync_phase)}}</div><div class="l">Sync phase · ${{sel.sync_progress_percent || 0}}%</div></div>
    <div class="stat"><div class="v">${{escapeHtml(sel.workstation_status || "—")}}</div><div class="l">Workstation</div></div>
    <div class="stat"><div class="v">${{sel.input_file_count}} / ${{sel.output_file_count}}</div><div class="l">Input / Output files</div></div>
    <div class="stat"><div class="v">${{sel.is_input_ready ? "YES" : "NO"}}</div><div class="l">InputReady gate</div></div>`;
  document.getElementById("pathHint").textContent =
    "Agent session folder: " + (sel.agent_session_hint || "") +
    (sel.sync_message ? " · " + sel.sync_message : "");

  const files = []
    .concat((sel.input_files || []).map(f => ({{...f, role:"INPUT"}})))
    .concat((sel.output_files || []).map(f => ({{...f, role:"OUTPUT"}})));
  document.getElementById("fileRows").innerHTML = files.map(f =>
    `<tr><td>${{f.role}}</td><td class="mono">${{escapeHtml(f.relative_path)}}</td>
     <td class="mono">${{escapeHtml((f.sha256||"").slice(0,16))}}${{f.sha256 ? "..." : ""}}</td>
     <td>${{f.size}}</td><td>${{escapeHtml(f.source)}}</td></tr>`
  ).join("") || `<tr><td colspan="5">No files</td></tr>`;

  document.getElementById("cmdRows").innerHTML = (sel.commands || []).map(c =>
    `<tr><td>${{escapeHtml(c.type)}}</td><td>${{escapeHtml(c.status)}}</td>
     <td class="mono">${{escapeHtml(c.result_message || c.error_message || "")}}</td>
     <td class="mono">${{escapeHtml(c.created_at || "")}}</td>
     <td class="mono">${{escapeHtml(c.completed_at || "")}}</td></tr>`
  ).join("") || `<tr><td colspan="5">No commands</td></tr>`;

  document.getElementById("eventLog").textContent = (sel.events || []).map(e =>
    `${{e.created_at || ""}}  ${{e.success === false ? "FAIL" : "OK  "}}  ${{e.details || e.action}}`
  ).join("\\n");
  document.getElementById("xferLog").textContent = (sel.transfers || []).map(t =>
    `${{t.direction}} ${{t.status}} retries=${{t.retry_count}} ${{t.checksum_expected ? "sha=" + t.checksum_expected.slice(0,12) : ""}} ${{t.error_message || ""}}`
  ).join("\\n");
}}

async function refresh(forceSelect) {{
  const q = new URLSearchParams();
  if (selectedId) q.set("workspace_id", selectedId);
  const res = await fetch(API_BASE + "?" + q.toString(), {{
    headers: {{ "Accept": "application/json" }},
    credentials: "same-origin",
  }});
  if (!res.ok) {{ flash("Refresh failed: " + res.status, false); return; }}
  state = await res.json();
  if (forceSelect && state.selected) selectedId = state.selected.id;
  if (!selectedId && state.selected) selectedId = state.selected.id;
  render();
}}

async function postAction(body, isForm) {{
  const opts = {{
    method: "POST",
    headers: {{ "X-CSRFToken": csrftoken }},
    credentials: "same-origin",
  }};
  if (isForm) {{
    opts.body = body;
  }} else {{
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }}
  const res = await fetch(ACTION_URL, opts);
  const data = await res.json().catch(() => ({{}}));
  if (!res.ok) {{
    flash(data.detail || ("Action failed: " + res.status), false);
    return null;
  }}
  flash(data.command_type ? (data.command_type + " queued") : "OK", true);
  if (data.workspace && data.workspace.id) selectedId = data.workspace.id;
  await refresh(true);
  return data;
}}

document.getElementById("btnCreate").onclick = () => postAction({{
  action: "create",
  booking_id: document.getElementById("booking").value,
  workstation_id: document.getElementById("workstation").value,
  ingest: false,
}});
document.getElementById("btnPrepare").onclick = () => {{
  if (!selectedId) return flash("Select a workspace", false);
  postAction({{ action: "prepare", workspace_id: selectedId }});
}};
document.getElementById("btnCollect").onclick = () => {{
  if (!selectedId) return flash("Select a workspace", false);
  postAction({{ action: "collect", workspace_id: selectedId }});
}};
document.getElementById("btnCleanup").onclick = () => {{
  if (!selectedId) return flash("Select a workspace", false);
  postAction({{ action: "cleanup", workspace_id: selectedId }});
}};
document.getElementById("btnRefresh").onclick = () => refresh(true);
document.getElementById("btnUpload").onclick = async () => {{
  if (!selectedId) return flash("Create/select workspace first", false);
  const f = document.getElementById("fileInput").files[0];
  if (!f) return flash("Choose a file", false);
  const fd = new FormData();
  fd.append("action", "upload");
  fd.append("workspace_id", selectedId);
  fd.append("folder", "RawData");
  fd.append("file", f);
  await postAction(fd, true);
}};
document.getElementById("btnLogs").onclick = () => {{
  const sel = state.selected;
  const text = [
    "=== Portal lifecycle events ===",
    document.getElementById("eventLog").textContent,
    "",
    "=== Transfers ===",
    document.getElementById("xferLog").textContent,
    "",
    "Agent log (on workstation): C:\\\\ProgramData\\\\RemoteAnalysisAgent\\\\Logs\\\\raa-YYYYMMDD.log",
    "Look for: Polling, CommandReceived, WorkspaceCreated, Downloading, DownloadComplete, WaitingForAnalysis, Uploading, UploadVerified, Cleanup, Idle",
    sel && sel.agent_session_hint ? ("Session path: " + sel.agent_session_hint) : "",
  ].join("\\n");
  const w = window.open("", "_blank", "width=900,height=700");
  w.document.write("<pre style='font:12px monospace;white-space:pre-wrap;padding:16px'></pre>");
  w.document.querySelector("pre").textContent = text;
}};

render();
setInterval(() => refresh(false), 5000);
</script>
</body>
</html>"""
