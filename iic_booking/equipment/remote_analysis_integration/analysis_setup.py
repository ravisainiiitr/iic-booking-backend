"""Analysis setup popup + transfer status for the Remote Analysis workspace redesign (see CONTRACT.md)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from django.db import transaction
from django.db.models import Count, F, Min, Q
from django.utils import timezone

from iic_booking.communication.email_branding import local_date
from iic_booking.equipment.models import Booking, BookingResultFile
from iic_booking.equipment.remote_analysis_integration import research
from iic_booking.equipment.remote_analysis_integration.raw_staging import (
    BookingRawStagingService,
    cached_results_s3_objects,
)
from iic_booking.remote_analysis.constants import TransferDirection, TransferStatus, WorkspaceSyncPhase
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, BookingAnalysisSetup, WorkspaceTransfer

logger = logging.getLogger(__name__)

BOOKING_DETAILS_LABEL = "Booking Details › Analyzed Data"
INPUT_SOURCES = {"booking", "upload"}
ACTIVE_POLL_MS = 2000
IDLE_POLL_MS = 12000
TRANSFER_PHASES = {"staging_input", "collecting", "copying_to_workspace", "verifying", "cleaning_pc"}


class SetupError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _now_iso() -> str:
    return timezone.now().isoformat()


def merge_state(model, pk, field: str, **values) -> dict[str, Any]:
    """Merge keys into a JSON state column without touching other columns (no updated_at bump)."""
    with transaction.atomic():
        row = model.objects.select_for_update().filter(pk=pk).values_list(field, flat=True).first()
        state = dict(row or {})
        state.update(values)
        state["updated_at"] = _now_iso()
        model.objects.filter(pk=pk).update(**{field: state})
    return state


def current_workspace(booking) -> AnalysisWorkspace | None:
    reservation_id = getattr(booking, "analysis_reservation_id", None)
    qs = AnalysisWorkspace.objects.select_related("workstation", "research_link__workspace", "research_link__folder")
    if reservation_id:
        ws = qs.filter(reservation_id=reservation_id).first()
        if ws is not None:
            return ws
    return qs.filter(booking=booking).order_by("-created_at").first()


def open_session(booking):
    from iic_booking.remote_analysis.guacamole.authorization import OPEN_SESSION_STATUSES
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    return (
        RemoteDesktopSession.objects.filter(booking_id=booking.pk, status__in=OPEN_SESSION_STATUSES)
        .order_by("-created_at")
        .first()
    )


def get_setup(booking) -> BookingAnalysisSetup | None:
    return (
        BookingAnalysisSetup.objects.select_related(
            "research_link__workspace", "research_link__folder", "input_booking", "input_booking__equipment"
        )
        .filter(booking=booking)
        .first()
    )


def setup_link(setup: BookingAnalysisSetup | None, user):
    if setup is None or not research.research_available(user)[0]:
        return None
    return research.usable_link(setup.research_link, user)


def _first_slot(booking):
    if hasattr(booking, "first_slot_at"):
        first = booking.first_slot_at
    else:
        first = booking.daily_slots.aggregate(m=Min("start_datetime"))["m"]
    when = first or booking.created_at
    day = local_date(when)
    return day.isoformat() if day else None


def serialize_booking(booking) -> dict[str, Any]:
    equipment = booking.equipment
    return {
        "id": booking.booking_id,
        "virtual_id": booking.virtual_booking_id or None,
        "equipment_name": equipment.name if equipment else None,
        "equipment_code": equipment.code if equipment else None,
        "date": _first_slot(booking),
        "status": booking.status,
    }


def serialize_link(link) -> dict[str, Any] | None:
    if link is None:
        return None
    root, raw, processed = research.find_folders(link)
    from iic_booking.my_research.services import folder_breadcrumbs

    return {
        "link_id": link.pk,
        "workspace_id": str(link.workspace_id),
        "workspace_name": link.workspace.name,
        "folder_id": str(root.pk) if root else None,
        "folder_path": " / ".join([link.workspace.name] + [c["name"] for c in folder_breadcrumbs(root)]) if root else None,
        "raw_folder_id": str(raw.pk) if raw else None,
        "processed_folder_id": str(processed.pk) if processed else None,
    }


def _db_file_counts(booking_ids: list[int]) -> dict[int, int]:
    from iic_booking.sync.models import ResultAttachment

    counts: dict[int, int] = {}
    for booking_id, c in (
        BookingResultFile.objects.filter(booking_id__in=booking_ids).values("booking_id").annotate(c=Count("pk")).values_list("booking_id", "c")
    ):
        counts[booking_id] = counts.get(booking_id, 0) + c
    for booking_id, c in (
        ResultAttachment.objects.filter(result__booking_id__in=booking_ids)
        .values("result__booking_id")
        .annotate(c=Count("pk"))
        .values_list("result__booking_id", "c")
    ):
        counts[booking_id] = counts.get(booking_id, 0) + c
    return counts


def booking_file_count(booking, db_counts: dict[int, int] | None = None) -> int:
    counts = db_counts if db_counts is not None else _db_file_counts([booking.booking_id])
    db = counts.get(booking.booking_id, 0)
    if db:
        return db
    vid = (booking.virtual_booking_id or "").strip()
    return len(cached_results_s3_objects(vid, prefix_only=True)) if vid else 0


def _selected_input(setup: BookingAnalysisSetup | None, link, workspace) -> dict[str, Any] | None:
    if setup is None:
        return None
    if setup.input_source == "booking" and setup.input_booking is not None:
        ib = setup.input_booking
        return {
            "source": "booking",
            "booking_id": ib.booking_id,
            "virtual_id": ib.virtual_booking_id or None,
            "file_count": booking_file_count(ib),
        }
    if link is not None:
        files, _ = research.raw_data_files(link)
        count = files.count()
    elif workspace is not None:
        count = workspace.files.filter(deleted=False, is_current=True, relative_path__startswith="RawData/").count()
    else:
        count = 0
    return {"source": "upload", "booking_id": None, "virtual_id": None, "file_count": count}


def _default_input_booking_id(booking, user) -> int | None:
    if booking_file_count(booking) > 0:
        return booking.booking_id
    page = input_sources(user, booking, page_size=1)
    return page["results"][0]["booking_id"] if page["results"] else booking.booking_id


def _agent(workspace, booking) -> dict[str, Any]:
    ws = getattr(workspace, "workstation", None) if workspace is not None else None
    if ws is None:
        reservation = getattr(booking, "analysis_reservation", None)
        ws = getattr(reservation, "workstation", None) if reservation is not None else None
    if ws is None:
        return {"version": None, "capabilities": []}
    return {"version": ws.agent_version or None, "capabilities": list(ws.agent_capabilities or [])}


def agent_capabilities(workstation) -> set[str]:
    return set((getattr(workstation, "agent_capabilities", None) or []))


def setup_payload(booking, user) -> dict[str, Any]:
    from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService
    from iic_booking.my_research.access import can_create_workspace
    from iic_booking.my_research.models import ResearchWorkspace, ResearchWorkspaceBooking, WorkspaceStatus

    eligible, reason = research.research_available(user)
    setup = get_setup(booking)
    link = setup_link(setup, user)
    workspace = current_workspace(booking)
    workspaces: list[dict[str, Any]] = []
    if eligible:
        linked = set(
            ResearchWorkspaceBooking.objects.filter(booking=booking, workspace__owner=user).values_list("workspace_id", flat=True)
        )
        workspaces = [
            {"id": str(w.pk), "name": w.name, "booking_linked": w.pk in linked}
            for w in ResearchWorkspace.objects.filter(owner=user, status=WorkspaceStatus.ACTIVE).order_by("name")
        ]
    destination = research.processed_destination(link) if link is not None else None
    ws_for_output = getattr(workspace, "workstation", None) if workspace is not None else None
    if ws_for_output is None:
        reservation = getattr(booking, "analysis_reservation", None)
        ws_for_output = getattr(reservation, "workstation", None) if reservation is not None else None
    return {
        "booking": serialize_booking(booking),
        "my_research": {
            "eligible": eligible,
            "reason": reason,
            "current_link": serialize_link(link),
            "workspaces": workspaces,
            "can_create": bool(eligible and can_create_workspace(user)),
        },
        "folders_preview": {
            "root": research.booking_folder_name(booking),
            "raw": research.RAW_FOLDER,
            "processed": research.PROCESSED_FOLDER,
        },
        "input": {
            "default_source": "booking",
            "default_booking_id": _default_input_booking_id(booking, user),
            "selected": _selected_input(setup, link, workspace),
            "pc_input_path": (getattr(ws_for_output, "input_path", "") or None) if ws_for_output else None,
        },
        "output": {
            "pc_output_path": (getattr(ws_for_output, "output_path", "") or None) if ws_for_output else None,
            "destination_label": f"My Research › {destination['path_label']}" if destination else BOOKING_DETAILS_LABEL,
            "auto_delete_after_verify": "verified_cleanup_v1" in agent_capabilities(ws_for_output),
        },
        "agent": _agent(workspace, booking),
        "eligibility": BookingAnalysisEligibilityService().evaluate(booking).as_dict(),
    }


def _resolve_project(user, data: dict[str, Any], booking):
    from iic_booking.my_research.access import can_create_workspace
    from iic_booking.my_research.models import (
        ActivityAction,
        MemberRole,
        ResearchWorkspace,
        ResearchWorkspaceMember,
        WorkspaceStatus,
    )
    from iic_booking.my_research.services import link_booking, record_activity

    raw_id = data.get("workspace_id")
    new_name = str(data.get("new_workspace_name") or "").strip()
    project = None
    if raw_id:
        try:
            project_id = uuid.UUID(str(raw_id))
        except (TypeError, ValueError):
            raise SetupError("invalid_workspace", "Choose one of your My Research projects.") from None
        project = ResearchWorkspace.objects.filter(pk=project_id, owner=user, status=WorkspaceStatus.ACTIVE).first()
        if project is None:
            raise SetupError("invalid_workspace", "Choose one of your active My Research projects.")
    elif new_name:
        from iic_booking.my_research import file_policy

        name = file_policy.strip_control_chars(new_name).strip()[:200]
        if not name:
            raise SetupError("invalid_workspace", "Project name is required.")
        project = ResearchWorkspace.objects.filter(owner=user, status=WorkspaceStatus.ACTIVE, name__iexact=name).first()
        if project is None:
            if not can_create_workspace(user):
                raise SetupError("not_eligible", "Creating My Research projects is not available for your account yet.")
            project = ResearchWorkspace.objects.create(owner=user, name=name)
            ResearchWorkspaceMember.objects.create(workspace=project, user=user, role=MemberRole.OWNER, added_by=user)
            record_activity(
                project, user, ActivityAction.WORKSPACE_CREATED, target_type="workspace", target_id=project.pk, target_label=name
            )
    if project is None:
        return None
    link, _ = link_booking(project, booking, user)
    research.ensure_folders(link, booking, user)
    return link


@transaction.atomic
def apply_setup(booking, user, data: dict[str, Any]) -> BookingAnalysisSetup:
    eligible, reason = research.research_available(user)
    wants_project = bool(data.get("workspace_id") or str(data.get("new_workspace_name") or "").strip())
    if wants_project and not eligible:
        raise SetupError("not_eligible", reason or "My Research is not available for your account.")
    input_source = str(data.get("input_source") or "booking").strip().lower()
    if input_source not in INPUT_SOURCES:
        raise SetupError("invalid_input", "input_source must be 'booking' or 'upload'.")

    input_booking = None
    if input_source == "booking":
        raw_input = data.get("input_booking_id") or booking.booking_id
        try:
            input_id = int(raw_input)
        except (TypeError, ValueError):
            raise SetupError("input_booking_not_owned", "Choose one of your own bookings.") from None
        input_booking = (
            booking
            if input_id == booking.booking_id
            else Booking.objects.select_related("equipment").filter(booking_id=input_id, user=user).first()
        )
        if input_booking is None:
            raise SetupError("input_booking_not_owned", "Choose one of your own bookings.")
        code, message = research.results_lock(input_booking)
        if code:
            raise SetupError("results_locked", message or "Results of this booking are locked.")

    existing = BookingAnalysisSetup.objects.select_for_update().filter(booking=booking).first()
    link = _resolve_project(user, data, booking) if eligible else None
    if link is None and eligible and existing is not None and not wants_project:
        link = research.usable_link(existing.research_link, user)
        if link is not None:
            research.ensure_folders(link, booking, user)

    setup = existing or BookingAnalysisSetup(booking=booking, user=user)
    setup.user = user
    setup.research_link = link
    setup.input_source = input_source
    setup.input_booking = input_booking
    setup.save()

    workspace = current_workspace(booking)
    if workspace is not None and workspace.research_link_id != (link.pk if link else None):
        workspace.research_link = link
        workspace.save(update_fields=["research_link", "updated_at"])

    setup_id = str(setup.pk)
    merge_state(BookingAnalysisSetup, setup.pk, "state", phase="queued", message="", errors=[])

    def _queue():
        from iic_booking.remote_analysis.tasks import run_analysis_setup

        try:
            run_analysis_setup.delay(setup_id)
        except Exception:  # noqa: BLE001
            logger.exception("Could not queue analysis setup %s", setup_id)

    transaction.on_commit(_queue)
    return setup


def run_setup(setup_id: str) -> dict[str, Any]:
    """Celery body: copy booking raw data into My Research and pre-stage the analysis workspace."""
    setup = BookingAnalysisSetup.objects.select_related("booking", "booking__equipment", "user", "input_booking").filter(pk=setup_id).first()
    if setup is None:
        return {"skipped": "missing"}
    booking, user = setup.booking, setup.user
    link = setup_link(setup, user)
    result: dict[str, Any] = {}

    def _progress(state: dict[str, Any]) -> None:
        merge_state(BookingAnalysisSetup, setup.pk, "state", phase="copying", **state)

    try:
        if link is not None and setup.input_source == "booking" and setup.input_booking is not None:
            result["copy"] = research.copy_booking_raw(booking, setup.input_booking, link, actor=user, progress=_progress)
        workspace = current_workspace(booking)
        if workspace is not None and open_session(booking) is None:
            result["stage"] = stage_input(booking, workspace, actor=user, setup=setup, copy=False)
    except research.ResearchCopyError as exc:
        merge_state(BookingAnalysisSetup, setup.pk, "state", phase="failed", message=exc.detail, errors=[exc.detail])
        return {"error": exc.code}
    errors = (result.get("copy") or {}).get("errors") or []
    merge_state(
        BookingAnalysisSetup,
        setup.pk,
        "state",
        phase="done" if not errors else "failed",
        message="" if not errors else errors[0],
        errors=errors[:10],
        current_file="",
    )
    return result


def stage_input(
    booking,
    workspace,
    *,
    actor=None,
    setup: BookingAnalysisSetup | None = None,
    copy: bool = True,
    request=None,
) -> dict[str, Any]:
    """Fill the analysis workspace RawData from the setup choice (legacy: this booking's results)."""
    setup = setup if setup is not None else get_setup(booking)
    if setup is None:
        return BookingRawStagingService().stage_into_workspace(booking, workspace, actor=actor, request=request)
    link = setup_link(setup, setup.user)
    if link is not None:
        if workspace.research_link_id != link.pk:
            workspace.research_link = link
            workspace.save(update_fields=["research_link", "updated_at"])
        if copy and setup.input_source == "booking" and setup.input_booking is not None:
            try:
                research.copy_booking_raw(booking, setup.input_booking, link, actor=setup.user)
            except research.ResearchCopyError as exc:
                logger.warning("Raw copy before staging skipped for booking %s: %s", booking.pk, exc.detail)
        return research.stage_research_raw(workspace, link, actor=actor)
    if setup.input_source == "upload":
        return {"staged": 0, "skipped": 0, "errors": [], "total_source_files": 0, "success": True}
    return BookingRawStagingService().stage_into_workspace(
        setup.input_booking or booking, workspace, actor=actor, request=request
    )


def input_sources(user, booking, *, q: str = "", page: int = 1, page_size: int = 20) -> dict[str, Any]:
    from iic_booking.equipment.booking_results_service import booking_has_results_annotation

    page_size = max(1, min(int(page_size or 20), 50))
    page = max(1, int(page or 1))
    qs = (
        Booking.objects.filter(user=user)
        .select_related("equipment")
        .annotate(has_results_db=booking_has_results_annotation())
        .filter(Q(has_results_db=True) | Q(results_available_notified_at__isnull=False) | Q(booking_id=booking.booking_id))
    )
    q = (q or "").strip()
    if q:
        match = Q(virtual_booking_id__icontains=q) | Q(equipment__name__icontains=q) | Q(equipment__code__icontains=q)
        if q.isdigit():
            match |= Q(booking_id=int(q))
        qs = qs.filter(match)
    qs = qs.annotate(first_slot_at=Min("daily_slots__start_datetime")).order_by(
        F("first_slot_at").desc(nulls_last=True), "-booking_id"
    )
    rows = list(qs[(page - 1) * page_size : page * page_size + 1])
    has_more = len(rows) > page_size
    rows = rows[:page_size]
    counts = _db_file_counts([b.booking_id for b in rows])
    results = []
    for b in rows:
        file_count = booking_file_count(b, counts)
        if file_count < 1:
            continue
        code, message = research.results_lock(b)
        results.append(
            {
                "booking_id": b.booking_id,
                "virtual_id": b.virtual_booking_id or None,
                "equipment_name": b.equipment.name if b.equipment else None,
                "date": _first_slot(b),
                "status": b.status,
                "file_count": file_count,
                "is_current": b.booking_id == booking.booking_id,
                "locked_reason": message if code else None,
            }
        )
    count = qs.count() if (has_more or page > 1) else (len(results))
    return {"count": count, "results": results}


def _progress_fields(state: dict[str, Any] | None) -> dict[str, Any]:
    state = state or {}
    bytes_total = int(state.get("bytes_total") or 0)
    bytes_done = min(int(state.get("bytes_done") or 0), bytes_total) if bytes_total else int(state.get("bytes_done") or 0)
    return {
        "percent": int(bytes_done * 100 / bytes_total) if bytes_total else None,
        "bytes_done": bytes_done,
        "bytes_total": bytes_total,
        "files_done": int(state.get("files_done") or 0),
        "files_total": int(state.get("files_total") or 0),
        "current_file": state.get("current_file") or None,
    }


def _collect_progress(workspace, ts: dict[str, Any]) -> dict[str, Any]:
    collect = ts.get("collect") or {}
    if collect.get("transfer_id") and WorkspaceTransfer.objects.filter(
        pk=collect["transfer_id"], status__in=[TransferStatus.IN_PROGRESS, TransferStatus.PENDING]
    ).exists():
        return _progress_fields(collect)
    from iic_booking.remote_analysis.constants import CommandType
    from iic_booking.remote_analysis.models import RemoteCommand

    started = (
        RemoteCommand.objects.filter(command_type=CommandType.COLLECT_WORKSPACE, payload__workspace_id=str(workspace.id))
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    received = 0
    if started is not None:
        received = WorkspaceTransfer.objects.filter(
            workspace=workspace,
            direction=TransferDirection.AGENT_PUSH,
            status=TransferStatus.COMPLETED,
            file__isnull=False,
            created_at__gte=started,
        ).count()
    return {"percent": None, "bytes_done": 0, "bytes_total": 0, "files_done": received, "files_total": 0, "current_file": None}


def sync_status(booking, user) -> dict[str, Any]:
    setup = get_setup(booking)
    link = setup_link(setup, user)
    workspace = current_workspace(booking)
    ts = dict((workspace.transfer_state if workspace is not None else None) or {})
    setup_state = dict((setup.state if setup is not None else None) or {})
    phase, direction, message = "idle", None, ""
    progress = _progress_fields(None)
    progress["percent"] = None
    updated = [setup.updated_at if setup else None, workspace.updated_at if workspace else None]

    sp = workspace.sync_phase if workspace is not None else ""
    bridge = ts.get("bridge") or {}
    session = open_session(booking) if workspace is not None else None
    if workspace is None or sp in {"", WorkspaceSyncPhase.CANCELLED} or (sp == WorkspaceSyncPhase.PREPARING and session is None):
        if setup_state.get("phase") in {"queued", "copying"}:
            phase, direction, message = "staging_input", "input", "Copying booking data to My Research"
            progress = _progress_fields(setup_state)
        elif setup_state.get("phase") == "failed":
            phase, message = "failed", setup_state.get("message") or "Copying booking data failed"
    elif sp in {WorkspaceSyncPhase.PREPARING, WorkspaceSyncPhase.DOWNLOADING_INPUT, WorkspaceSyncPhase.VERIFYING_INPUT}:
        phase, direction, message = "staging_input", "input", workspace.sync_message or "Preparing input files"
    elif sp in {WorkspaceSyncPhase.INPUT_READY, WorkspaceSyncPhase.SESSION_STARTING}:
        phase, message = "ready", workspace.sync_message or "Input ready"
    elif sp == WorkspaceSyncPhase.SESSION_ACTIVE:
        phase, message = "in_session", workspace.sync_message or "Session active"
        if bridge.get("phase") == "running":
            phase, direction, message = "copying_to_workspace", "output", "Copying results to My Research"
            progress = _progress_fields(bridge)
    elif sp in {WorkspaceSyncPhase.COLLECTING_OUTPUT, WorkspaceSyncPhase.UPLOADING_OUTPUT, WorkspaceSyncPhase.RETRY_PENDING}:
        phase, direction, message = "collecting", "output", workspace.sync_message or "Collecting results"
        progress = _collect_progress(workspace, ts)
    elif sp == WorkspaceSyncPhase.UPLOAD_VERIFIED:
        phase, direction, message = "verifying", "output", workspace.sync_message or "Verifying results"
    elif sp == WorkspaceSyncPhase.CLEANUP:
        phase, direction, message = "cleaning_pc", "output", "Cleaning the analysis PC"
    elif sp == WorkspaceSyncPhase.COMPLETED:
        if bridge.get("phase") in {"queued", "running"}:
            phase, direction, message = "copying_to_workspace", "output", "Copying results to My Research"
            progress = _progress_fields(bridge)
        elif bridge.get("phase") == "failed":
            phase, direction, message = "failed", "output", bridge.get("message") or "Copying results to My Research failed"
        else:
            phase, message = "done", workspace.sync_message or "Results saved"
    elif sp in {WorkspaceSyncPhase.PREPARATION_FAILED, WorkspaceSyncPhase.UPLOAD_FAILED, WorkspaceSyncPhase.CLEANUP_FAILED}:
        phase, message = "failed", workspace.sync_message or "Transfer failed"
        direction = "input" if sp == WorkspaceSyncPhase.PREPARATION_FAILED else "output"

    if link is not None:
        destination = research.processed_destination(link)
    else:
        destination = {"workspace_id": None, "folder_id": None, "path_label": BOOKING_DETAILS_LABEL}
    extra_folders: list[dict[str, Any]] = []
    if workspace is not None and (ts.get("extra_sources") or (ts.get("collect") or {}).get("extra_sources")):
        from iic_booking.equipment.remote_analysis_integration.pc_folders import status_folders
        from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

        sid = str(session.id) if session is not None else WorkspaceSyncService().last_collect_session_id(workspace)
        extra_folders = status_folders(workspace, sid)
    stamps = [u for u in updated if u is not None]
    return {
        "phase": phase,
        "direction": direction,
        **progress,
        "message": message,
        "verified": bool(workspace is not None and workspace.upload_verified_at and phase in {"done", "copying_to_workspace", "cleaning_pc", "in_session"}),
        "pc_cleanup": ts.get("pc_cleanup") or "pending",
        "kept_files": list(ts.get("kept_files") or []),
        "pc_deleted": int(ts.get("pc_deleted") or 0),
        "pc_removed_folders": list(ts.get("pc_removed_folders") or []) if extra_folders else [],
        "pc_profile_wiped": ts.get("pc_profile_wiped") if extra_folders else None,
        "extra_folders": extra_folders,
        "destination": destination,
        "updated_at": (max(stamps).isoformat() if stamps else _now_iso()),
        "poll_after_ms": ACTIVE_POLL_MS if phase in TRANSFER_PHASES else IDLE_POLL_MS,
    }


def sync_now(booking, user) -> bool:
    """Mid-session collect for the open session. False when there is no active session."""
    from iic_booking.remote_analysis.workspace.sync import COLLECT_IN_FLIGHT_PHASES, WorkspaceSyncService

    session = open_session(booking)
    workspace = current_workspace(booking)
    if session is None or workspace is None or not workspace.workstation_id:
        return False
    if workspace.sync_phase in COLLECT_IN_FLIGHT_PHASES:
        return True
    WorkspaceSyncService().issue_collect_command(workspace, actor=user, session_id=str(session.id))
    return True


def queue_bridge(workspace, *, session_id: str, paths: list[str] | None, session_ended: bool) -> None:
    """After a verified collect: copy output to My Research (linked eligible owners only)."""
    if not workspace.research_link_id:
        return
    link = research.usable_link(workspace.research_link, workspace.user)
    if link is None or not research.research_available(workspace.user)[0]:
        return
    merge_state(AnalysisWorkspace, workspace.pk, "transfer_state", bridge={"phase": "queued", "session_id": session_id})
    workspace_id = str(workspace.pk)

    def _queue():
        from iic_booking.remote_analysis.tasks import bridge_workspace_output

        try:
            bridge_workspace_output.delay(workspace_id, session_id, paths, session_ended)
        except Exception:  # noqa: BLE001
            logger.exception("Could not queue My Research bridge for workspace %s", workspace_id)

    transaction.on_commit(_queue)


def run_bridge(workspace_id: str, session_id: str = "", paths: list[str] | None = None, session_ended: bool = True) -> dict[str, Any]:
    workspace = AnalysisWorkspace.objects.select_related("user", "booking", "reservation", "research_link__workspace").filter(pk=workspace_id).first()
    if workspace is None or not workspace.research_link_id:
        return {"skipped": "no_link"}
    link = research.usable_link(workspace.research_link, workspace.user)
    if link is None:
        merge_state(AnalysisWorkspace, workspace.pk, "transfer_state", bridge={"phase": "skipped"})
        return {"skipped": "link_unusable"}

    def _progress(state: dict[str, Any]) -> None:
        merge_state(AnalysisWorkspace, workspace.pk, "transfer_state", bridge={"phase": "running", "session_id": session_id, **state})

    result = research.bridge_processed(workspace, link, paths=set(paths) if paths is not None else None, progress=_progress)
    ok = not result["errors"]
    purged = 0
    if ok and session_ended:
        purged = research.purge_bridged_local(workspace, result["bridged"])
    first_failed_at = None
    if not ok:
        previous = (AnalysisWorkspace.objects.filter(pk=workspace.pk).values_list("transfer_state", flat=True).first() or {})
        first_failed_at = ((previous.get("bridge") or {}).get("first_failed_at")) or _now_iso()
    merge_state(
        AnalysisWorkspace,
        workspace.pk,
        "transfer_state",
        bridge={
            "phase": "done" if ok else "failed",
            "session_id": session_id,
            "files_total": result["total"],
            "files_done": result["copied"] + result["skipped"],
            "copied": result["copied"],
            "skipped": result["skipped"],
            "purged": purged,
            "message": "" if ok else result["errors"][0],
            "errors": result["errors"][:10],
            "first_failed_at": first_failed_at,
        },
    )
    return {k: v for k, v in result.items() if k != "bridged"} | {"purged": purged}
