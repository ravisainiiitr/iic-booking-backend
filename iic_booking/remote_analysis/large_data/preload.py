"""Phase 1: the reserved Analysis PC pre-loads the booking's raw data into a locked holding folder.

When a reservation holds a PC (RESERVED / AWAITING_CHECKIN / PREPARING / READY) and its input is known, the portal
queues ``PRELOAD_INPUT {reservation_id}``. Changing the input in the setup popup re-queues it with a new revision;
cancellation, no-show, the end of the reservation or a move to another PC queue ``DISCARD_PRELOAD {reservation_id}`` on
the PC that holds the data. The agent fetches the manifest (fresh presigned URLs every call) and reports progress.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import CommandStatus, CommandType, ReservationStatus
from iic_booking.remote_analysis.large_data import s3
from iic_booking.remote_analysis.large_data.common import (
    CAP_PRELOAD,
    URL_LIFETIME_SECONDS,
    TransferError,
    agent_supports,
    as_int,
    check_size_limit,
    eta_seconds,
    iso,
    observed_rate,
    transfer_limits,
)
from iic_booking.remote_analysis.transfer_models import InputPreload, PreloadState

logger = logging.getLogger(__name__)

HOLD_STATUSES = {
    ReservationStatus.RESERVED,
    ReservationStatus.AWAITING_CHECKIN,
    ReservationStatus.PREPARING,
    ReservationStatus.READY,
}
AGENT_STATES = {PreloadState.QUEUED, PreloadState.DOWNLOADING, PreloadState.READY, PreloadState.FAILED}
LIVE_STATES = {PreloadState.QUEUED, PreloadState.DOWNLOADING, PreloadState.READY}

UrlBuilder = Callable[[str], str]


# --- queueing -----------------------------------------------------------------------------------------------------


def _paths(reservation_id) -> dict[str, str]:
    base = f"/api/v1/analysis/agent/transfers/input/{reservation_id}"
    return {"manifest_url": f"{base}/manifest/", "progress_url": f"{base}/progress/"}


def input_known(booking) -> bool:
    if booking is None:
        return False
    from iic_booking.remote_analysis.workspace_models import BookingAnalysisSetup

    if BookingAnalysisSetup.objects.filter(booking=booking).exists():
        return True
    try:
        from iic_booking.equipment.remote_analysis_integration.analysis_setup import booking_file_count

        return booking_file_count(booking) > 0
    except Exception:  # noqa: BLE001
        logger.exception("Could not count input files for booking %s", booking.pk)
        return False


def _queue(preload: InputPreload, workstation, *, reason: str) -> None:
    from iic_booking.remote_analysis.services.commands import CommandService

    _expire_pending(preload)
    cmd = CommandService().create_command(
        workstation,
        CommandType.PRELOAD_INPUT,
        payload={
            "reservation_id": str(preload.reservation_id),
            "revision": preload.revision,
            "reason": reason,
            **_paths(preload.reservation_id),
            "limits": transfer_limits(workstation),
        },
    )
    now = timezone.now()
    preload.workstation = workstation
    preload.command = cmd
    preload.state = PreloadState.QUEUED
    preload.files_done = preload.bytes_done = 0
    preload.detail = ""
    preload.queued_at = now
    preload.ready_at = None
    preload.discarded_at = None
    preload.save()


def _expire_pending(preload: InputPreload) -> None:
    from iic_booking.remote_analysis.models import RemoteCommand

    if preload.command_id:
        RemoteCommand.objects.filter(pk=preload.command_id, status=CommandStatus.PENDING).update(
            status=CommandStatus.EXPIRED
        )


def discard(preload: InputPreload, *, reason: str) -> None:
    from iic_booking.remote_analysis.services.commands import CommandService

    if preload.state == PreloadState.DISCARDED:
        return
    _expire_pending(preload)
    if preload.workstation_id:
        try:
            CommandService().create_command(
                preload.workstation,
                CommandType.DISCARD_PRELOAD,
                payload={"reservation_id": str(preload.reservation_id), "revision": preload.revision, "reason": reason},
            )
        except Exception:  # noqa: BLE001
            logger.exception("Could not queue DISCARD_PRELOAD for reservation %s", preload.reservation_id)
    preload.state = PreloadState.DISCARDED
    preload.detail = reason[:300]
    preload.discarded_at = timezone.now()
    preload.save(update_fields=["state", "detail", "discarded_at", "updated_at"])


@transaction.atomic
def sync_reservation(reservation, *, input_changed: bool = False) -> InputPreload | None:
    """Bring the pre-load in line with where the reservation is now. Idempotent."""
    preload = InputPreload.objects.select_for_update().filter(reservation=reservation).first()
    ws = reservation.workstation if reservation.workstation_id else None
    holding = reservation.status in HOLD_STATUSES and ws is not None

    if not holding:
        if reservation.status == ReservationStatus.ACTIVE and ws is not None and preload and preload.workstation_id == ws.pk:
            return preload  # the session is using it (PLACE_INPUT moves it out of the holding folder)
        if preload is not None and preload.state != PreloadState.DISCARDED:
            reason = {
                ReservationStatus.CANCELLED: "cancelled",
                ReservationStatus.EXPIRED: "expired",
                ReservationStatus.FAILED: "failed",
                ReservationStatus.COMPLETED: "completed",
            }.get(reservation.status, "released")
            discard(preload, reason=reason)
        return preload

    if preload is not None and preload.state != PreloadState.DISCARDED and preload.workstation_id not in (None, ws.pk):
        discard(preload, reason="moved")
    if not agent_supports(ws, CAP_PRELOAD) or not input_known(reservation.booking if reservation.booking_id else None):
        return preload
    if preload is None:
        preload = InputPreload.objects.create(reservation=reservation, workstation=ws)
        _queue(preload, ws, reason="allocated")
    elif preload.state == PreloadState.DISCARDED or preload.workstation_id != ws.pk:
        preload.revision += 1
        _queue(preload, ws, reason="allocated")
    elif input_changed:
        preload.revision += 1
        _queue(preload, ws, reason="input_changed")
    return preload


def sync_reservation_id(reservation_id, *, input_changed: bool = False) -> None:
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    reservation = AnalysisReservation.objects.select_related("workstation", "booking").filter(pk=reservation_id).first()
    if reservation is None:
        return
    try:
        sync_reservation(reservation, input_changed=input_changed)
    except Exception:  # noqa: BLE001 - pre-load is an optimisation; it must never block a reservation change
        logger.exception("Pre-load sync failed for reservation %s", reservation_id)


def on_input_changed(booking) -> None:
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    ids = list(
        AnalysisReservation.objects.filter(booking=booking, status__in=HOLD_STATUSES | {ReservationStatus.ACTIVE}).values_list(
            "pk", flat=True
        )
    )
    for reservation_id in ids:
        sync_reservation_id(reservation_id, input_changed=True)


# --- manifest -----------------------------------------------------------------------------------------------------


def _file_id(prefix: str, *parts: Any) -> str:
    digest = hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}-{digest}"


def _booking_entries(input_booking) -> list[dict[str, Any]]:
    from iic_booking.equipment.booking_results_service import CONTROL_RESULT_FILE_NAMES
    from iic_booking.equipment.remote_analysis_integration import research
    from iic_booking.equipment.remote_analysis_integration.raw_staging import (
        BookingRawStagingService,
        is_material_entry,
    )

    rows = []
    for entry in BookingRawStagingService().list_raw_entries(input_booking):
        name = str(entry.get("name") or "")
        if Path(name).name.strip().lower() in CONTROL_RESULT_FILE_NAMES or not is_material_entry(entry):
            continue
        rel = name.replace("\\", "/").lstrip("/")
        if not rel or ".." in rel.split("/"):
            continue
        key = research._s3_source_key(input_booking, entry) if s3.results_bucket() else ""
        identity = key or entry.get("attachment_id") or entry.get("file_id") or rel
        sha = str(entry.get("sha256") or "").lower()
        rows.append(
            {
                "file_id": _file_id("bk", input_booking.pk, identity),
                "name": Path(rel).name,
                "rel_path": rel,
                "size": int(entry.get("size_bytes") or 0),
                "sha256": sha if len(sha) == 64 else None,
                "_bucket_key": key,
                "_entry": entry,
                "_booking": input_booking,
                "source": "booking",
            }
        )
    return rows


def _research_entries(link) -> list[dict[str, Any]]:
    from iic_booking.equipment.remote_analysis_integration import research

    files, raw = research.raw_data_files(link)
    if raw is None:
        return []
    paths = research._folder_relative_paths(raw)
    rows = []
    for rf in files.order_by("display_name"):
        prefix = paths.get(rf.folder_id, "")
        rel = f"{prefix}/{rf.display_name}" if prefix else rf.display_name
        sha = research.b64_to_hex(rf.checksum_sha256)
        rows.append(
            {
                "file_id": f"mr-{rf.pk}",
                "name": rf.display_name,
                "rel_path": rel,
                "size": int(rf.size_bytes or 0),
                "sha256": sha or None,
                "_research_file": rf,
                "source": "my_research",
            }
        )
    return rows


def _workspace_entries(reservation) -> list[dict[str, Any]]:
    from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceFile

    workspace = AnalysisWorkspace.objects.filter(reservation=reservation).first()
    if workspace is None and reservation.booking_id:
        workspace = AnalysisWorkspace.objects.filter(booking_id=reservation.booking_id).order_by("-created_at").first()
    if workspace is None:
        return []
    rows = []
    for wf in WorkspaceFile.objects.filter(
        workspace=workspace, deleted=False, is_current=True, relative_path__startswith="RawData/"
    ).order_by("relative_path"):
        rel = wf.relative_path.split("/", 1)[1]
        rows.append(
            {
                "file_id": f"ws-{wf.pk}",
                "name": Path(rel).name,
                "rel_path": rel,
                "size": int(wf.size or 0),
                "sha256": (wf.sha256 or "").lower() or None,
                "_workspace_file": wf,
                "_workspace": workspace,
                "source": "upload",
            }
        )
    return rows


def input_entries(reservation) -> list[dict[str, Any]]:
    """Every file of the booking's chosen input, with private ``_`` fields used to sign URLs or stream."""
    from iic_booking.equipment.remote_analysis_integration.analysis_setup import get_setup, setup_link

    booking = reservation.booking if reservation.booking_id else None
    if booking is None:
        return []
    setup = get_setup(booking)
    link = setup_link(setup, setup.user) if setup is not None else None
    if link is not None:
        rows = _research_entries(link)
        phase = ((setup.state or {}).get("phase") or "") if setup is not None else ""
        if setup.input_source == "booking" and setup.input_booking_id and phase not in {"done"}:
            # The copy into My Research is still running: take the rest straight from the booking results.
            have = {(r["rel_path"].lower(), r["size"]) for r in rows}
            rows += [r for r in _booking_entries(setup.input_booking) if (r["rel_path"].lower(), r["size"]) not in have]
        return rows
    if setup is not None and setup.input_source == "upload":
        return _workspace_entries(reservation)
    input_booking = setup.input_booking if setup is not None and setup.input_booking_id else booking
    return _booking_entries(input_booking)


def content_path(reservation_id, file_id: str) -> str:
    return f"/api/v1/analysis/agent/transfers/input/{reservation_id}/files/{file_id}/content/"


def _sign(row: dict[str, Any], reservation_id, build_url: UrlBuilder) -> tuple[str, str | None, bool]:
    """(url, expires_at, needs_agent_auth)."""
    from iic_booking.my_research import storage as research_storage

    expires = iso(timezone.now() + timedelta(seconds=URL_LIFETIME_SECONDS))
    rf = row.get("_research_file")
    if rf is not None:
        url = research_storage.presign_get(
            rf.storage_key,
            filename=rf.display_name,
            disposition="attachment",
            content_type="application/octet-stream",
            expires_in=URL_LIFETIME_SECONDS,
        )
        return url, expires, False
    key = row.get("_bucket_key")
    if key:
        return s3.presign_get(s3.results_bucket(), key, expires_in=URL_LIFETIME_SECONDS), expires, False
    # Legacy files kept on the portal (operator uploads, old DSA copies, browser uploads to the workspace).
    return build_url(content_path(reservation_id, row["file_id"])), None, True


def manifest(reservation, *, build_url: UrlBuilder) -> dict[str, Any]:
    rows = input_entries(reservation)
    total = sum(int(r["size"] or 0) for r in rows)
    check_size_limit(total)
    files = []
    for row in rows:
        try:
            url, expires, agent_auth = _sign(row, reservation.pk, build_url)
        except s3.StorageError as exc:
            raise TransferError("storage_error", f"Could not sign a download link: {exc}", 503) from exc
        files.append(
            {
                "file_id": row["file_id"],
                "name": row["name"],
                "rel_path": row["rel_path"],
                "size": row["size"],
                "sha256": row["sha256"],
                "url": url,
                "url_expires_at": expires,
                "auth": "agent" if agent_auth else None,
                "source": row["source"],
            }
        )
    preload = InputPreload.objects.filter(reservation=reservation).first()
    return {
        "reservation_id": str(reservation.pk),
        "revision": preload.revision if preload else None,
        "total_bytes": total,
        "files": files,
        "limits": transfer_limits(reservation.workstation if reservation.workstation_id else None),
    }


def open_content(reservation, file_id: str):
    """(file object, size, name) for a legacy portal-stored input file, or None."""
    from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
    from iic_booking.remote_analysis.workspace.storage import StorageManager

    row = next((r for r in input_entries(reservation) if r["file_id"] == file_id), None)
    if row is None or row.get("_research_file") is not None or row.get("_bucket_key"):
        return None
    wf = row.get("_workspace_file")
    if wf is not None:
        path = StorageManager().read_file(row["_workspace"], wf.storage_relpath)
        if not path.exists():
            return None
        return open(path, "rb"), path.stat().st_size, row["name"]
    opened = BookingRawStagingService()._open(row["_booking"], row["_entry"])
    if opened is None:
        return None
    fileobj, _sha, size = opened
    return fileobj, size, row["name"]


# --- progress -----------------------------------------------------------------------------------------------------


def record_progress(preload: InputPreload, data: dict[str, Any]) -> InputPreload:
    state = str(data.get("state") or "").strip().lower()
    if state not in AGENT_STATES:
        raise TransferError("invalid_state", "state must be queued, downloading, ready or failed.")
    if preload.state == PreloadState.DISCARDED:
        raise TransferError("discarded", "This pre-load was discarded; delete the holding folder.", 409)
    revision = data.get("revision")
    if revision not in (None, "") and as_int(revision) and as_int(revision) < preload.revision:
        raise TransferError("stale_revision", "The input changed; fetch the manifest again.", 409, revision=preload.revision)
    now = timezone.now()
    bytes_done = as_int(data.get("bytes_done"))
    preload.rate_bps = observed_rate(preload.bytes_done, preload.last_progress_at, bytes_done, now, preload.rate_bps)
    preload.state = state
    preload.files_done = as_int(data.get("files_done"))
    preload.files_total = as_int(data.get("files_total"))
    preload.bytes_done = bytes_done
    preload.bytes_total = as_int(data.get("bytes_total"))
    preload.detail = str(data.get("detail") or "")[:300]
    preload.last_progress_at = now
    if state == PreloadState.READY and preload.ready_at is None:
        preload.ready_at = now
    preload.save()
    return preload


def payload_for(reservation) -> dict[str, Any] | None:
    """``session.input_preload`` for the experience payload."""
    if reservation is None:
        return None
    preload = InputPreload.objects.filter(reservation=reservation).first()
    if preload is None or preload.state == PreloadState.DISCARDED:
        return None
    fallback = None
    if preload.workstation_id:
        from iic_booking.remote_analysis.large_data.speedtest import latest_for

        latest = latest_for(workstation=preload.workstation)
        fallback = latest.download_mbps if latest is not None else None
    return {
        "state": preload.state,
        "files_done": preload.files_done,
        "files_total": preload.files_total,
        "bytes_done": preload.bytes_done,
        "bytes_total": preload.bytes_total,
        "eta_seconds": None
        if preload.state in {PreloadState.READY, PreloadState.FAILED}
        else eta_seconds(preload.bytes_done, preload.bytes_total, preload.rate_bps, fallback),
        "detail": preload.detail or None,
    }


def staging_for_placement(workspace) -> InputPreload | None:
    """The live pre-load PLACE_INPUT can move from (same PC, not discarded)."""
    if workspace is None or not workspace.reservation_id:
        return None
    return InputPreload.objects.filter(
        reservation_id=workspace.reservation_id, workstation_id=workspace.workstation_id, state__in=LIVE_STATES
    ).first()
