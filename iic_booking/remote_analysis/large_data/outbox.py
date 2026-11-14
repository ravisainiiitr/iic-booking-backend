"""Phase 2: results outbox. At End Analysis the agent moves the chosen results into a locked outbox and finishes the
session at once; it then uploads them in the background straight to My Research (presigned S3, resumable).

Files land in ``<Project>/<VID>/Processed Data`` exactly as the ``agent-upload/`` + bridge path puts them today (same
``ResearchFile`` rows, origin ``analysis_output``, same folder, sub-folders kept, clashes become "name (2).ext").
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.utils import timezone

from iic_booking.my_research import storage as research_storage
from iic_booking.remote_analysis.large_data.common import (
    MAX_PART_URLS_PER_CALL,
    PART_SIZE,
    URL_LIFETIME_SECONDS,
    TransferError,
    as_int,
    check_size_limit,
    clean_rel_path,
    clean_sha256,
    eta_seconds,
    observed_rate,
    part_size_for,
)
from iic_booking.remote_analysis.transfer_models import OutputFileStatus, OutputState, OutputUpload, OutputUploadFile

logger = logging.getLogger(__name__)

AGENT_STATES = {OutputState.QUEUED, OutputState.UPLOADING, OutputState.PAUSED, OutputState.FAILED}
OPEN_STATES = {OutputState.QUEUED, OutputState.UPLOADING, OutputState.PAUSED}
STALL_MINUTES = 30
MAX_FILES = 20_000
FINISHED = (OutputFileStatus.DONE, OutputFileStatus.SKIPPED)


def _research():
    from iic_booking.equipment.remote_analysis_integration import research

    return research


# --- context ------------------------------------------------------------------------------------------------------


def _workspace(session):
    from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

    if session.reservation_id:
        ws = AnalysisWorkspace.objects.select_related("research_link__workspace", "user").filter(
            reservation_id=session.reservation_id
        ).first()
        if ws is not None:
            return ws
    if session.booking_id:
        return (
            AnalysisWorkspace.objects.select_related("research_link__workspace", "user")
            .filter(booking_id=session.booking_id)
            .order_by("-created_at")
            .first()
        )
    return None


def _link(session):
    """The My Research project link the results go to, or raise ``no_research_link``."""
    research = _research()
    workspace = _workspace(session)
    user = session.user
    link = research.usable_link(workspace.research_link, user) if workspace is not None and workspace.research_link_id else None
    if link is None:
        from iic_booking.equipment.remote_analysis_integration.analysis_setup import get_setup, setup_link

        booking = session.booking if session.booking_id else None
        setup = get_setup(booking) if booking is not None else None
        link = setup_link(setup, user) if setup is not None else None
    if link is None or not research.research_available(user)[0] or not research_storage.storage_configured():
        raise TransferError(
            "no_research_link",
            "This booking is not linked to a My Research project; upload the results with agent-upload/ instead.",
            409,
        )
    return link


def outbox_for(session) -> OutputUpload | None:
    return OutputUpload.objects.filter(session=session).first()


# --- init / parts / state / complete --------------------------------------------------------------------------------


def _parse_files(raw) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_FILES:
        raise TransferError("invalid_files", f"files must be a list of 1 to {MAX_FILES} entries.")
    seen = set()
    files = []
    for item in raw:
        if not isinstance(item, dict):
            raise TransferError("invalid_files", "Each file needs rel_path, size and sha256.")
        rel = clean_rel_path(item.get("rel_path"))
        sha = clean_sha256(item.get("sha256"))
        if rel is None or not sha:
            raise TransferError("invalid_files", "Each file needs a relative rel_path and a hex sha256.")
        if rel.lower() in seen:
            raise TransferError("invalid_files", f"Duplicate rel_path: {rel}")
        seen.add(rel.lower())
        files.append({"rel_path": rel, "size": as_int(item.get("size")), "sha256": sha})
    return files


def _target_folder(link, booking, rel: str, actor):
    research = _research()
    _, _, processed = research.ensure_folders(link, booking, actor)
    dir_parts, leaf = research._split_relative(rel)
    folder = research._subfolder(link.workspace, processed, dir_parts, actor)
    return folder, leaf


def _already_in_research(link, folder, leaf: str, sha_hex: str) -> bool:
    from iic_booking.my_research.models import FileOrigin, FileStatus, ResearchFile

    research = _research()
    return ResearchFile.objects.filter(
        workspace=link.workspace,
        folder=folder,
        status=FileStatus.AVAILABLE,
        origin=FileOrigin.ANALYSIS_OUTPUT,
        original_name=leaf[:255],
        checksum_sha256=research.hex_to_b64(sha_hex),
    ).exists()


def _file_payload(f: OutputUploadFile) -> dict[str, Any]:
    done = f.status in FINISHED
    single = not done and not f.s3_upload_id
    url, headers = (None, None)
    if single:
        url, headers = research_storage.presign_put(
            f.key,
            content_type="application/octet-stream",
            expires_in=URL_LIFETIME_SECONDS,
            checksum_sha256_b64=_research().hex_to_b64(f.sha256),
        )
    return {
        "file_id": str(f.id),
        "rel_path": f.rel_path,
        "key": f.key,
        "upload_id": f.s3_upload_id or None,
        "part_size": f.part_size,
        "single_put_url": url,
        "put_headers": headers,
        "done": done,
        "skipped": f.status == OutputFileStatus.SKIPPED,
    }


def init(session, data: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.my_research.services import quota_error

    research = _research()
    files = _parse_files(data.get("files"))
    total = sum(f["size"] for f in files)
    check_size_limit(total)
    link = _link(session)
    booking = session.booking if session.booking_id else None
    actor = session.user

    with transaction.atomic():
        outbox, _ = OutputUpload.objects.select_for_update().get_or_create(
            session=session,
            defaults={
                "workstation_id": session.workstation_id,
                "booking": booking,
                "user": actor,
                "research_link": link,
            },
        )
        existing = {f.rel_path.lower(): f for f in outbox.files.all()}
        new_bytes = sum(f["size"] for f in files if f["rel_path"].lower() not in existing)
        over = quota_error(link.workspace, new_bytes) if new_bytes else None
        if over:
            raise TransferError("quota", over, 409)
        rows: list[OutputUploadFile] = []
        for item in files:
            row = existing.get(item["rel_path"].lower())
            if row is not None and row.status in FINISHED:
                rows.append(row)
                continue
            if row is not None and (row.sha256 != item["sha256"] or row.size != item["size"]):
                # The file changed in the outbox: start it again.
                _abort(row)
                row.delete()
                row = None
            if row is None:
                folder, leaf = _target_folder(link, booking, item["rel_path"], actor)
                row = OutputUploadFile(outbox=outbox, rel_path=item["rel_path"], size=item["size"], sha256=item["sha256"])
                if _already_in_research(link, folder, leaf, item["sha256"]):
                    row.status = OutputFileStatus.SKIPPED
                    row.completed_at = timezone.now()
                else:
                    from iic_booking.my_research import file_policy

                    row.key = research_storage.build_object_key(
                        link.workspace_id, row.planned_research_id, file_policy.storage_safe_name(leaf) or "file"
                    )
                    row.part_size = part_size_for(item["size"])
                    if item["size"] >= PART_SIZE:
                        row.s3_upload_id = research_storage.create_multipart_upload(
                            row.key, content_type="application/octet-stream"
                        )
                try:
                    with transaction.atomic():
                        row.save()
                except IntegrityError:
                    row = outbox.files.get(rel_path=item["rel_path"])
            rows.append(row)
        outbox.research_link = link
        if outbox.state in {OutputState.FAILED, OutputState.COMPLETE} and any(r.status not in FINISHED for r in rows):
            outbox.state = OutputState.QUEUED
        outbox.save()
        _refresh_counts(outbox)
    if outbox.state != OutputState.COMPLETE and not outbox.files.exclude(status__in=FINISHED).exists():
        finish(outbox)
    return {"session_id": str(session.id), "files": [_file_payload(r) for r in rows]}


def _get_file(outbox: OutputUpload, file_id) -> OutputUploadFile:
    try:
        row = outbox.files.filter(pk=file_id).first()
    except (ValueError, Exception):  # noqa: BLE001 - malformed uuid
        row = None
    if row is None:
        raise TransferError("unknown_file", "Unknown file_id; call init/ again.", 404)
    return row


def _require_outbox(session) -> OutputUpload:
    outbox = outbox_for(session)
    if outbox is None:
        raise TransferError("not_initialised", "Call init/ first.", 409)
    return outbox


def parts(session, data: dict[str, Any]) -> dict[str, Any]:
    outbox = _require_outbox(session)
    row = _get_file(outbox, data.get("file_id"))
    if row.status in FINISHED:
        return {"file_id": str(row.id), "urls": {}, "done": True}
    if not row.s3_upload_id:
        raise TransferError("single_put", "This file uses single_put_url; call init/ for a fresh one.", 409)
    numbers = data.get("part_numbers")
    needed = max(1, -(-row.size // row.part_size)) if row.part_size else 1
    if not isinstance(numbers, list) or not numbers or len(numbers) > MAX_PART_URLS_PER_CALL:
        raise TransferError("invalid_parts", f"part_numbers must list 1 to {MAX_PART_URLS_PER_CALL} parts.")
    urls = {}
    for raw in numbers:
        n = as_int(raw)
        if n < 1 or n > needed:
            raise TransferError("invalid_parts", f"Part {raw} is outside 1..{needed}.")
        urls[str(n)] = research_storage.presign_upload_part(
            row.key, upload_id=row.s3_upload_id, part_number=n, expires_in=URL_LIFETIME_SECONDS
        )
    return {"file_id": str(row.id), "urls": urls, "url_lifetime_seconds": URL_LIFETIME_SECONDS}


def state(session) -> dict[str, Any]:
    outbox = _require_outbox(session)
    files = []
    for row in outbox.files.order_by("rel_path"):
        done = row.status in FINISHED
        parts_done: list[int] = []
        reset = False
        if not done and row.s3_upload_id:
            try:
                parts_done = [p["part_number"] for p in research_storage.list_uploaded_parts(row.key, upload_id=row.s3_upload_id)]
            except research_storage.ObjectNotFound:
                # Aborted (e.g. the 7-day lifecycle rule): start a fresh multipart upload.
                row.s3_upload_id = research_storage.create_multipart_upload(row.key, content_type="application/octet-stream")
                row.save(update_fields=["s3_upload_id"])
                reset = True
        files.append(
            {
                "file_id": str(row.id),
                "rel_path": row.rel_path,
                "done": done,
                "parts_done": sorted(parts_done),
                "upload_id": row.s3_upload_id or None,
                "restarted": reset,
            }
        )
    return {"session_id": str(session.id), "state": outbox.state, "files": files}


def _abort(row: OutputUploadFile) -> None:
    if row.s3_upload_id:
        try:
            research_storage.abort_multipart_upload(row.key, upload_id=row.s3_upload_id)
        except research_storage.ResearchStorageError:
            logger.warning("Could not abort multipart upload for outbox file %s", row.pk)


def complete(session, data: dict[str, Any]) -> dict[str, Any]:
    from iic_booking.remote_analysis.large_data import s3

    outbox = _require_outbox(session)
    row = _get_file(outbox, data.get("file_id"))
    if row.status in FINISHED:
        return {"file_id": str(row.id), "done": True, "research_file_id": str(row.research_file_id or "") or None}
    sha = clean_sha256(data.get("sha256"))
    if sha and sha != row.sha256:
        raise TransferError("checksum_mismatch", "sha256 differs from the one sent to init/; call init/ again.", 409)
    verified = False
    if row.s3_upload_id:
        part_list = s3.parse_parts(data.get("parts"))
        if part_list is None:
            raise TransferError("invalid_parts", "parts must be [{n, etag}] for every uploaded part.")
        try:
            research_storage.complete_multipart_upload(row.key, upload_id=row.s3_upload_id, parts=part_list)
        except research_storage.ObjectNotFound:
            pass  # already completed (a lost response); the head check below decides
    else:
        verified = True  # the single PUT was signed with the file's SHA-256, so S3 checked it
    try:
        head = research_storage.head_object(row.key)
    except research_storage.ObjectNotFound as exc:
        raise TransferError("not_uploaded", "The file is not in storage yet.", 409) from exc
    size = int(head.get("ContentLength") or 0)
    if size != row.size:
        research_storage.delete_object(row.key)
        row.s3_upload_id = research_storage.create_multipart_upload(row.key, content_type="application/octet-stream") if row.size >= PART_SIZE else ""
        row.save(update_fields=["s3_upload_id"])
        raise TransferError("size_mismatch", f"Stored size {size} != {row.size}; upload the file again.", 409)
    research_file = _register(outbox, row, verified=verified, etag=str(head.get("ETag") or "").strip('"'))
    _refresh_counts(outbox)
    if not outbox.files.exclude(status__in=FINISHED).exists():
        finish(outbox)
    return {"file_id": str(row.id), "done": True, "research_file_id": str(research_file.pk)}


@transaction.atomic
def _register(outbox: OutputUpload, row: OutputUploadFile, *, verified: bool, etag: str):
    """Record the uploaded object in My Research exactly as the agent-upload/ bridge does."""
    from iic_booking.my_research import file_policy
    from iic_booking.my_research.models import FileOrigin, FileStatus, ResearchFile

    research = _research()
    row = OutputUploadFile.objects.select_for_update().get(pk=row.pk)
    if row.status in FINISHED and row.research_file_id:
        return row.research_file
    link = outbox.research_link
    if link is None:
        raise TransferError("no_research_link", "The My Research project link is gone.", 409)
    booking = outbox.booking
    actor = outbox.user
    folder, leaf = _target_folder(link, booking, row.rel_path, actor)
    research_file = ResearchFile(
        id=row.planned_research_id,
        workspace=link.workspace,
        folder=folder,
        booking=booking,
        original_name=leaf[:255],
        display_name=research.unique_display_name(link.workspace, folder, file_policy.clean_display_name(leaf) or "file")[:255],
        storage_key=row.key,
        size_bytes=row.size,
        status=FileStatus.PENDING_UPLOAD,
        origin=FileOrigin.ANALYSIS_OUTPUT,
        uploaded_by=actor if getattr(actor, "pk", None) else None,
    )
    research_file.save()
    try:
        research._finalize_research_file(
            research_file,
            checksum_b64=research.hex_to_b64(row.sha256),
            verified=verified,
            etag=etag,
            actor=actor,
            any_type=True,
        )
    except research.ResearchCopyError as exc:
        raise TransferError(exc.code, exc.detail, 409) from exc
    row.status = OutputFileStatus.DONE
    row.research_file = research_file
    row.completed_at = timezone.now()
    row.save(update_fields=["status", "research_file", "completed_at"])
    return research_file


def _refresh_counts(outbox: OutputUpload) -> None:
    agg = outbox.files.aggregate(
        total=Count("id"),
        done=Count("id", filter=Q(status__in=FINISHED)),
        bytes_total=Sum("size"),
        bytes_done=Sum("size", filter=Q(status__in=FINISHED)),
    )
    current = OutputUpload.objects.filter(pk=outbox.pk).values_list("bytes_done", flat=True).first() or 0
    OutputUpload.objects.filter(pk=outbox.pk).update(
        files_total=agg["total"] or 0,
        files_done=agg["done"] or 0,
        bytes_total=agg["bytes_total"] or 0,
        bytes_done=max(current, agg["bytes_done"] or 0),
    )
    outbox.refresh_from_db()


def progress(session, data: dict[str, Any]) -> OutputUpload:
    outbox = _require_outbox(session)
    state_value = str(data.get("state") or OutputState.UPLOADING).strip().lower()
    if state_value not in AGENT_STATES:
        raise TransferError("invalid_state", "state must be queued, uploading, paused or failed.")
    if outbox.state == OutputState.COMPLETE:
        return outbox
    now = timezone.now()
    bytes_done = as_int(data.get("bytes_done"))
    outbox.rate_bps = observed_rate(outbox.bytes_done, outbox.last_progress_at, bytes_done, now, outbox.rate_bps)
    outbox.bytes_done = bytes_done
    outbox.bytes_total = as_int(data.get("bytes_total")) or outbox.bytes_total
    outbox.files_done = max(outbox.files_done, as_int(data.get("files_done")))
    outbox.files_total = as_int(data.get("files_total")) or outbox.files_total
    outbox.state = state_value
    outbox.detail = str(data.get("detail") or "")[:300]
    if state_value != OutputState.PAUSED:
        outbox.paused_reason = ""
    else:
        outbox.paused_reason = outbox.detail or "Paused by the Analysis PC"
    outbox.last_progress_at = now
    outbox.save()
    return outbox


# --- completion, notices, stall -----------------------------------------------------------------------------------


def destination_label(outbox: OutputUpload) -> str | None:
    if outbox.research_link_id is None:
        return None
    dest = _research().processed_destination(outbox.research_link)
    return f"My Research › {dest['path_label']}" if dest else None


def my_research_href(outbox: OutputUpload) -> str | None:
    """Frontend link to the booking's Processed Data folder (``/my-research/<project>?folder=<id>``)."""
    if outbox.research_link_id is None:
        return None
    dest = _research().processed_destination(outbox.research_link)
    if not dest:
        return None
    href = f"/my-research/{dest['workspace_id']}"
    return f"{href}?folder={dest['folder_id']}" if dest.get("folder_id") else href


def finish(outbox: OutputUpload) -> None:
    with transaction.atomic():
        locked = OutputUpload.objects.select_for_update().get(pk=outbox.pk)
        if locked.state == OutputState.COMPLETE:
            return
        _refresh_counts(locked)
        locked.state = OutputState.COMPLETE
        locked.bytes_done = locked.bytes_total
        locked.files_done = locked.files_total
        locked.completed_at = timezone.now()
        locked.paused_reason = ""
        locked.save()
    outbox.refresh_from_db()
    _mark_workspace_done(outbox)
    transaction.on_commit(lambda pk=outbox.pk: notify_complete(pk))


def _mark_workspace_done(outbox: OutputUpload) -> None:
    try:
        from iic_booking.remote_analysis.constants import WorkspaceSyncPhase
        from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

        workspace = _workspace(outbox.session)
        if workspace is None:
            return
        workspace.upload_verified_at = timezone.now()
        workspace.save(update_fields=["upload_verified_at", "updated_at"])
        WorkspaceSyncService().set_sync_phase(
            workspace, WorkspaceSyncPhase.COMPLETED, percent=100, message="Results uploaded to My Research"
        )
    except Exception:  # noqa: BLE001
        logger.exception("Could not mark workspace done for outbox %s", outbox.pk)


def notify_complete(outbox_id) -> None:
    """Bell + email + WhatsApp: "Your results are in My Research › …". Once per outbox; never raises."""
    outbox = OutputUpload.objects.select_related("session", "booking", "booking__equipment", "user").filter(pk=outbox_id).first()
    if outbox is None or outbox.notified_at is not None or outbox.user is None:
        return
    claimed = OutputUpload.objects.filter(pk=outbox.pk, notified_at__isnull=True).update(notified_at=timezone.now())
    if not claimed:
        return
    where = destination_label(outbox) or "My Research"
    booking = outbox.booking
    ref = (getattr(booking, "virtual_booking_id", None) or getattr(booking, "pk", "")) if booking else ""
    title = "Your analysis results are uploaded"
    body = (
        f"All {outbox.files_total} result file{'s' if outbox.files_total != 1 else ''} of booking {ref} "
        f"({_human(outbox.bytes_total)}) are in {where}."
    ).replace("booking  (", "your session (")
    link = my_research_href(outbox) or "/my-research"
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [outbox.user],
            title=title,
            message=body,
            link=link,
            notification_type="success",
            event="remote_analysis_results_uploaded",
            extra={"session_id": str(outbox.session_id)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Results-uploaded bell failed for outbox %s", outbox.pk)
    try:
        from iic_booking.remote_analysis.constants import NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine

        NotificationEngine().notify(
            outbox.user, NotificationType.UPLOAD_COMPLETE, title, body, link=link, metadata={"session_id": str(outbox.session_id)}
        )
    except Exception:  # noqa: BLE001
        logger.exception("Results-uploaded email failed for outbox %s", outbox.pk)
    try:
        from iic_booking.whatsapp import hooks

        hooks.remote_analysis_results_uploaded(outbox, where)
    except Exception:  # noqa: BLE001
        logger.exception("Results-uploaded WhatsApp failed for outbox %s", outbox.pk)


def _human(size: int) -> str:
    value = float(size or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def _staff_for(outbox: OutputUpload) -> list:
    booking = outbox.booking
    if booking is None or not booking.equipment_id:
        return []
    from iic_booking.equipment.models import EquipmentOperator

    return [op.operator for op in EquipmentOperator.objects.select_related("operator").filter(equipment_id=booking.equipment_id)]


def sweep_stalled(now=None) -> int:
    """No progress for 30 min while the PC is offline: mark ``paused`` and alert lab staff (once per outbox)."""
    from iic_booking.remote_analysis.services.availability import AvailabilityEngine

    now = now or timezone.now()
    cutoff = now - timedelta(minutes=STALL_MINUTES)
    engine = AvailabilityEngine()
    count = 0
    qs = OutputUpload.objects.select_related("workstation", "booking").filter(state__in=[OutputState.QUEUED, OutputState.UPLOADING])
    qs = qs.filter(Q(last_progress_at__lt=cutoff) | Q(last_progress_at__isnull=True, created_at__lt=cutoff))
    for outbox in qs:
        ws = outbox.workstation
        if ws is not None and engine.heartbeat_fresh(ws):
            continue
        name = getattr(ws, "hostname", "") or "The Analysis PC"
        outbox.state = OutputState.PAUSED
        outbox.paused_reason = f"{name} is offline; the upload resumes when it is back online."
        outbox.save(update_fields=["state", "paused_reason", "updated_at"])
        count += 1
        if outbox.staff_alerted_at is None:
            OutputUpload.objects.filter(pk=outbox.pk).update(staff_alerted_at=now)
            _alert_staff(outbox, name)
    return count


def _alert_staff(outbox: OutputUpload, name: str) -> None:
    ref = getattr(outbox.booking, "virtual_booking_id", "") if outbox.booking_id else ""
    message = (
        f"{name} has not uploaded results{f' for booking {ref}' if ref else ''} for {STALL_MINUTES} minutes and is offline "
        f"({outbox.files_done} of {outbox.files_total} files done). The results are safe in its outbox; please bring the "
        "PC back online so the upload can finish."
    )
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            _staff_for(outbox),
            title="Analysis PC results upload paused",
            message=message,
            link="/remote-analysis",
            notification_type="warning",
            event="remote_analysis_outbox_paused",
            extra={"session_id": str(outbox.session_id)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Outbox paused alert failed for %s", outbox.pk)
    try:
        from iic_booking.remote_analysis.constants import AuditCategory
        from iic_booking.remote_analysis.services.audit import record_event

        record_event(
            category=AuditCategory.COMMANDS,
            action="OutboxPaused",
            details=message[:500],
            success=False,
            workstation=outbox.workstation,
            correlation_id=str(outbox.session_id),
        )
    except Exception:  # noqa: BLE001
        logger.exception("Outbox paused audit failed for %s", outbox.pk)


def payload_for(booking) -> dict[str, Any] | None:
    """``output_upload`` for the experience/booking payload: the booking's latest outbox."""
    if booking is None:
        return None
    outbox = OutputUpload.objects.filter(booking=booking).order_by("-created_at").first()
    if outbox is None:
        return None
    fallback = None
    if outbox.workstation_id:
        from iic_booking.remote_analysis.large_data.speedtest import latest_for

        latest = latest_for(workstation=outbox.workstation)
        fallback = latest.upload_mbps if latest is not None else None
    return {
        "session_id": str(outbox.session_id),
        # The UI's names (contract v1.2): "done" instead of the agent-facing "complete".
        "state": "done" if outbox.state == OutputState.COMPLETE else outbox.state,
        "bytes_done": outbox.bytes_done,
        "bytes_total": outbox.bytes_total,
        "files_done": outbox.files_done,
        "files_total": outbox.files_total,
        "eta_seconds": None
        if outbox.state in {OutputState.COMPLETE, OutputState.FAILED, OutputState.PAUSED}
        else eta_seconds(outbox.bytes_done, outbox.bytes_total, outbox.rate_bps, fallback),
        "paused_reason": outbox.paused_reason or None,
        "destination_label": destination_label(outbox),
        "my_research_href": my_research_href(outbox),
        "completed_at": outbox.completed_at.isoformat() if outbox.completed_at else None,
    }


# --- collect hand-over --------------------------------------------------------------------------------------------


def handle_collect_complete(workspace, session_id: str, result: dict[str, Any] | None) -> bool:
    """COLLECT_WORKSPACE finished by moving results into the outbox: free the PC now; uploads follow.

    True when handled (the caller then skips the old verify-collected-files path).
    """
    from iic_booking.remote_analysis.constants import WorkspaceStatus, WorkspaceSyncPhase
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession
    from iic_booking.remote_analysis.workspace.sync import (
        WorkspaceSyncService,
        release_workstation_after_collect,
    )

    outbox_flag = isinstance(result, dict) and bool(result.get("outbox"))
    session = RemoteDesktopSession.objects.filter(pk=session_id).first() if session_id else None
    if session is None:
        return False
    has_outbox = OutputUpload.objects.filter(session=session).exists()
    if not outbox_flag and not has_outbox:
        return False
    if WorkspaceSyncService.session_is_open(str(session.pk)):
        return False  # mid-session sync keeps the old path
    if not has_outbox:
        OutputUpload.objects.get_or_create(
            session=session,
            defaults={
                "workstation_id": session.workstation_id,
                "booking_id": session.booking_id,
                "user_id": session.user_id,
                "files_total": as_int((result or {}).get("files_total")),
                "bytes_total": as_int((result or {}).get("bytes_total")),
            },
        )
    sync = WorkspaceSyncService()
    try:
        plan = sync.verify_collect_plan(workspace, str(session.pk))[0]
        if plan is not None:
            sync._close_collect_plan(plan, success=True)
    except Exception:  # noqa: BLE001
        logger.exception("Could not close collect plan for outbox session %s", session.pk)
    workspace.status = WorkspaceStatus.ACTIVE
    workspace.last_synced_at = timezone.now()
    workspace.save(update_fields=["status", "last_synced_at", "updated_at"])
    outbox = OutputUpload.objects.get(session=session)
    if outbox.state != OutputState.COMPLETE:
        # COMPLETED (not UPLOADING_OUTPUT, which holds the PC): the PC's part is done; output_upload tracks the rest.
        sync.set_sync_phase(
            workspace,
            WorkspaceSyncPhase.COMPLETED,
            percent=100,
            message="Results moved to the outbox; uploading to My Research in the background",
        )
    if workspace.workstation_id:
        release_workstation_after_collect(workspace.workstation, reason="Results moved to the outbox")
    return True
