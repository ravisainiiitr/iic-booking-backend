"""My Research for Remote Analysis: project folders, raw-data copy, input staging and the output bridge.

Layout inside the chosen project: ``<Project>/<VID>/Raw Data`` and ``<Project>/<VID>/Processed Data``.
Folders are looked up by name (case-insensitive) and recreated when renamed or deleted.
"""

from __future__ import annotations

import base64
import binascii
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Callable

from django.conf import settings
from django.core.files import File
from django.db import IntegrityError, transaction
from django.utils import timezone

from iic_booking.equipment.booking_results_service import CONTROL_RESULT_FILE_NAMES
from iic_booking.equipment.remote_analysis_integration.raw_staging import (
    BookingRawStagingService,
    spool_stream,
)
from iic_booking.my_research import file_policy
from iic_booking.my_research import storage as research_storage
from iic_booking.my_research.access import NOT_ELIGIBLE_MESSAGE, feature_enabled, is_eligible
from iic_booking.my_research.models import (
    ActivityAction,
    FileOrigin,
    FileStatus,
    ResearchFile,
    ResearchFolder,
    ResearchWorkspace,
    ResearchWorkspaceBooking,
    WorkspaceStatus,
)
from iic_booking.my_research.services import (
    IN_FLIGHT_STATUSES,
    active_folders,
    descendant_folder_ids,
    quota_error,
    record_activity,
)

logger = logging.getLogger(__name__)

RAW_FOLDER = "Raw Data"
PROCESSED_FOLDER = "Processed Data"
BRIDGE_PORTAL_FOLDER = "Processed"
VERIFIED_PUT_MAX_BYTES = 100 * 1024 * 1024

Progress = Callable[[dict[str, Any]], None]


class ResearchCopyError(Exception):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


def research_available(user) -> tuple[bool, str | None]:
    if not feature_enabled():
        return False, "My Research is not enabled."
    if not is_eligible(user):
        return False, NOT_ELIGIBLE_MESSAGE
    return True, None


def booking_folder_name(booking) -> str:
    return (booking.virtual_booking_id or f"booking-{booking.pk}").strip()[:255]


def hex_to_b64(hex_digest: str) -> str:
    try:
        return base64.b64encode(bytes.fromhex(hex_digest or "")).decode("ascii") if hex_digest else ""
    except ValueError:
        return ""


def b64_to_hex(b64_digest: str) -> str:
    try:
        return base64.b64decode(b64_digest or "").hex() if b64_digest else ""
    except (binascii.Error, ValueError):
        return ""


def usable_link(link: ResearchWorkspaceBooking | None, user) -> ResearchWorkspaceBooking | None:
    """The link when its project is still active and owned by user."""
    if link is None:
        return None
    workspace = link.workspace
    if workspace.owner_id != getattr(user, "pk", None) or workspace.status != WorkspaceStatus.ACTIVE:
        return None
    return link


def _child_folder(workspace: ResearchWorkspace, parent: ResearchFolder | None, name: str, actor) -> ResearchFolder:
    def _lookup():
        return active_folders(workspace).filter(parent=parent, name__iexact=name).order_by("created_at").first()

    existing = _lookup()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            folder = ResearchFolder.objects.create(workspace=workspace, parent=parent, name=name, created_by=actor)
    except IntegrityError:
        folder = _lookup()
        if folder is None:
            raise
        return folder
    record_activity(
        workspace,
        actor,
        ActivityAction.FOLDER_CREATED,
        target_type="folder",
        target_id=folder.pk,
        target_label=name,
        details={"parent_id": str(parent.pk) if parent else None, "source": "remote_analysis"},
    )
    return folder


def ensure_folders(link: ResearchWorkspaceBooking, booking, actor) -> tuple[ResearchFolder, ResearchFolder, ResearchFolder]:
    """Idempotently create ``<VID>/Raw Data`` and ``<VID>/Processed Data`` for a project link."""
    workspace = link.workspace
    root = link.folder if link.folder_id and link.folder.deleted_at is None else None
    if root is None:
        root = _child_folder(workspace, None, booking_folder_name(booking), actor)
        link.folder = root
        link.save(update_fields=["folder"])
    raw = _child_folder(workspace, root, RAW_FOLDER, actor)
    processed = _child_folder(workspace, root, PROCESSED_FOLDER, actor)
    return root, raw, processed


def find_folders(link: ResearchWorkspaceBooking | None) -> tuple[ResearchFolder | None, ResearchFolder | None, ResearchFolder | None]:
    """Read-only lookup (never creates)."""
    if link is None or not link.folder_id or link.folder.deleted_at is not None:
        return None, None, None
    root = link.folder
    children = {
        f.name.lower(): f
        for f in active_folders(link.workspace).filter(parent=root).order_by("-created_at")
    }
    return root, children.get(RAW_FOLDER.lower()), children.get(PROCESSED_FOLDER.lower())


def _subfolder(workspace, base: ResearchFolder, parts: list[str], actor) -> ResearchFolder:
    folder = base
    for part in parts:
        folder = _child_folder(workspace, folder, file_policy.clean_folder_name(part)[:255], actor)
    return folder


def _split_relative(name: str) -> tuple[list[str], str]:
    parts = [p for p in (name or "").replace("\\", "/").split("/") if p and p not in {".", ".."}]
    if not parts:
        return [], "file.bin"
    return parts[:-1], parts[-1]


def unique_display_name(workspace, folder, name: str) -> str:
    taken = {
        n.lower()
        for n in ResearchFile.objects.filter(
            workspace=workspace, folder=folder, status__in=IN_FLIGHT_STATUSES
        ).values_list("display_name", flat=True)
    }
    if name.lower() not in taken:
        return name
    stem, ext = os.path.splitext(name)
    index = 2
    while f"{stem} ({index}){ext}".lower() in taken:
        index += 1
    return f"{stem} ({index}){ext}"


def _folder_relative_paths(base: ResearchFolder) -> dict:
    """{folder_id: "sub/dir"} for base and its active descendants ("" for base)."""
    paths = {base.pk: ""}
    level = [base.pk]
    guard = 0
    while level and guard < 100:
        rows = list(
            ResearchFolder.objects.filter(parent_id__in=level, deleted_at__isnull=True).values_list(
                "pk", "parent_id", "name"
            )
        )
        level = []
        for pk, parent_id, name in rows:
            prefix = paths.get(parent_id, "")
            paths[pk] = f"{prefix}/{name}" if prefix else name
            level.append(pk)
        guard += 1
    return paths


def _new_research_file(workspace, folder, name: str, *, booking, actor, origin: str, size: int) -> ResearchFile:
    file_id = uuid.uuid4()
    display = unique_display_name(workspace, folder, file_policy.clean_display_name(name) or "file")[:255]
    return ResearchFile(
        id=file_id,
        workspace=workspace,
        folder=folder,
        booking=booking,
        original_name=(name or display)[:255],
        display_name=display,
        storage_key=research_storage.build_object_key(workspace.pk, file_id, file_policy.storage_safe_name(display)),
        size_bytes=int(size or 0),
        status=FileStatus.PENDING_UPLOAD,
        origin=origin,
        uploaded_by=actor if getattr(actor, "pk", None) else None,
    )


def _finalize_research_file(
    research_file: ResearchFile,
    *,
    checksum_b64: str,
    verified: bool,
    etag: str = "",
    actor=None,
    any_type: bool = False,
) -> None:
    head = research_storage.head_object(research_file.storage_key)
    size = int(head.get("ContentLength") or 0)
    if research_file.size_bytes and size != research_file.size_bytes:
        research_storage.delete_object(research_file.storage_key)
        raise ResearchCopyError("size_mismatch", f"{research_file.display_name}: stored size {size} != {research_file.size_bytes}")
    detected = file_policy.sniff_type(research_storage.read_prefix(research_file.storage_key, file_policy.SNIFF_BYTES))
    # Unknown/executable types are still only ever served as octet-stream attachments.
    reason = None if any_type else file_policy.rejection_reason(detected)
    if reason:
        research_storage.delete_object(research_file.storage_key)
        raise ResearchCopyError("rejected", f"{research_file.display_name}: {reason}")
    research_file.size_bytes = size
    research_file.detected_type = detected
    research_file.checksum_sha256 = checksum_b64 or ""
    research_file.checksum_verified = bool(verified and checksum_b64)
    research_file.etag = (etag or str(head.get("ETag") or "").strip('"'))[:200]
    research_file.etag_is_md5 = bool(research_file.etag) and "-" not in research_file.etag and not research_storage.uses_kms()
    research_file.status = FileStatus.AVAILABLE
    research_file.uploaded_at = timezone.now()
    research_file.save()
    record_activity(
        research_file.workspace,
        actor,
        ActivityAction.FILE_UPLOADED,
        target_type="file",
        target_id=research_file.pk,
        target_label=research_file.display_name,
        details={"size_bytes": research_file.size_bytes, "booking_id": research_file.booking_id, "origin": research_file.origin},
    )


def upload_verified(fileobj, research_file: ResearchFile, checksum_b64: str) -> bool:
    """Upload bytes; S3 validates the SHA-256 for single-request uploads. Returns True when validated."""
    if checksum_b64 and research_file.size_bytes <= VERIFIED_PUT_MAX_BYTES:
        research_storage.put_object_verified(fileobj, research_file.storage_key, checksum_sha256_b64=checksum_b64)
        return True
    research_storage.upload_fileobj(fileobj, research_file.storage_key)
    return False


def _results_bucket() -> str:
    return (getattr(settings, "AWS_STORAGE_BUCKET_NAME", "") or "").strip()


def _s3_source_key(booking, entry: dict[str, Any]) -> str:
    from iic_booking.equipment.booking_results_service import booking_result_attachments_qs

    key = (entry.get("s3_key") or entry.get("key") or "").strip()
    if key and not key.startswith(("dsa:", "booking_result:")) and str(entry.get("source") or "") in {"s3", "dsa"}:
        return key
    attachment_id = entry.get("attachment_id")
    if attachment_id:
        att = booking_result_attachments_qs(booking).filter(id=attachment_id).only("s3_key").first()
        if att and (att.s3_key or "").strip():
            return att.s3_key.strip()
    return ""


def results_lock(booking) -> tuple[str | None, str | None]:
    from iic_booking.equipment.results_sharing_views import _results_lock

    return _results_lock(booking)


def copy_booking_raw(
    booking,
    input_booking,
    link: ResearchWorkspaceBooking,
    *,
    actor,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Copy a booking's results into ``<VID>/Raw Data`` (S3 server-side where possible). Idempotent."""
    code, message = results_lock(input_booking)
    if code:
        raise ResearchCopyError("results_locked", message or "Results are locked for this booking.")
    workspace = link.workspace
    _, raw, _ = ensure_folders(link, booking, actor)
    entries = [
        e
        for e in BookingRawStagingService().list_raw_entries(input_booking, fresh=True)
        if Path(str(e.get("name") or "")).name.strip().lower() not in CONTROL_RESULT_FILE_NAMES
    ]
    bytes_total = sum(int(e.get("size_bytes") or 0) for e in entries)
    state = {"files_total": len(entries), "files_done": 0, "bytes_total": bytes_total, "bytes_done": 0, "current_file": ""}
    copied = skipped = 0
    errors: list[str] = []
    bucket = _results_bucket()

    for entry in entries:
        dir_parts, leaf = _split_relative(str(entry.get("name") or ""))
        size = int(entry.get("size_bytes") or 0)
        state["current_file"] = leaf
        if progress:
            progress(dict(state))
        try:
            folder = _subfolder(workspace, raw, dir_parts, actor)
            same_name = ResearchFile.objects.filter(
                workspace=workspace,
                folder=folder,
                status=FileStatus.AVAILABLE,
                origin=FileOrigin.BOOKING_RAW,
                booking=input_booking,
                original_name=leaf[:255],
            )
            source_key = _s3_source_key(input_booking, entry) if bucket else ""
            if source_key:
                if (size and same_name.filter(size_bytes=size).exists()) or (not size and same_name.exists()):
                    skipped += 1
                else:
                    over = quota_error(workspace, size)
                    if over:
                        raise ResearchCopyError("quota", over)
                    research_file = _new_research_file(
                        workspace, folder, leaf, booking=input_booking, actor=actor, origin=FileOrigin.BOOKING_RAW, size=size
                    )
                    result = research_storage.copy_from(bucket, source_key, research_file.storage_key, size_bytes=size)
                    _finalize_research_file(
                        research_file,
                        checksum_b64=result.get("checksum_sha256") or "",
                        verified=bool(result.get("checksum_sha256")),
                        etag=result.get("etag") or "",
                        actor=actor,
                    )
                    copied += 1
            else:
                with BookingRawStagingService().open_entry(input_booking, entry) as opened:
                    if opened is None:
                        raise ResearchCopyError("unavailable", f"{leaf}: unavailable")
                    fileobj, sha_hex, real_size = opened
                    checksum = hex_to_b64(sha_hex)
                    if checksum and same_name.filter(checksum_sha256=checksum).exists():
                        skipped += 1
                    else:
                        over = quota_error(workspace, real_size)
                        if over:
                            raise ResearchCopyError("quota", over)
                        research_file = _new_research_file(
                            workspace, folder, leaf, booking=input_booking, actor=actor, origin=FileOrigin.BOOKING_RAW, size=real_size
                        )
                        verified = upload_verified(fileobj, research_file, checksum)
                        _finalize_research_file(research_file, checksum_b64=checksum, verified=verified, actor=actor)
                        copied += 1
        except ResearchCopyError as exc:
            errors.append(exc.detail)
            if exc.code == "quota":
                break
        except research_storage.ResearchStorageError as exc:
            errors.append(f"{leaf}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("My Research raw copy failed for %s", leaf)
            errors.append(f"{leaf}: {exc}")
        state["files_done"] += 1
        state["bytes_done"] += size
    state["current_file"] = ""
    if progress:
        progress(dict(state))
    return {"copied": copied, "skipped": skipped, "errors": errors, "total": len(entries)}


def raw_data_files(link: ResearchWorkspaceBooking | None):
    _, raw, _ = find_folders(link)
    if raw is None:
        return ResearchFile.objects.none(), raw
    return (
        ResearchFile.objects.filter(folder_id__in=descendant_folder_ids(raw), status=FileStatus.AVAILABLE),
        raw,
    )


def stage_research_raw(workspace, link: ResearchWorkspaceBooking, *, actor=None) -> dict[str, Any]:
    """Stage ``<VID>/Raw Data`` (incl. subfolders) into the analysis workspace RawData for PREPARE."""
    from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
    from iic_booking.remote_analysis.workspace_models import WorkspaceFile

    booking = workspace.booking or getattr(workspace.reservation, "booking", None)
    _, raw, _ = ensure_folders(link, booking, actor or workspace.user)
    paths = _folder_relative_paths(raw)
    files = ResearchFile.objects.filter(folder_id__in=list(paths), status=FileStatus.AVAILABLE).order_by("display_name")
    staged = skipped = 0
    errors: list[str] = []
    mgr = TransferManager()
    for research_file in files:
        prefix = paths.get(research_file.folder_id, "")
        rel = f"{prefix}/{research_file.display_name}" if prefix else research_file.display_name
        expected_hex = b64_to_hex(research_file.checksum_sha256)
        existing = WorkspaceFile.objects.filter(
            workspace=workspace, relative_path=f"RawData/{rel}", deleted=False, is_current=True
        ).first()
        if existing and expected_hex and (existing.sha256 or "").lower() == expected_hex:
            skipped += 1
            continue
        try:
            fileobj, sha_hex, _size = spool_stream(research_storage.open_stream(research_file.storage_key))
            try:
                if expected_hex and sha_hex != expected_hex:
                    raise ResearchCopyError("checksum_mismatch", f"{rel}: checksum does not match My Research")
                mgr.upload(
                    workspace,
                    File(fileobj, name=rel.split("/")[-1]),
                    folder="RawData",
                    actor=actor,
                    expected_sha256=sha_hex,
                    source="booking_raw",
                    relative_name=rel,
                    override_quota=True,
                )
            finally:
                fileobj.close()
            staged += 1
        except (ResearchCopyError,) as exc:
            errors.append(exc.detail)
        except (TransferError, research_storage.ResearchStorageError) as exc:
            errors.append(f"{rel}: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("My Research staging failed for %s", rel)
            errors.append(f"{rel}: {exc}")
    return {"staged": staged, "skipped": skipped, "errors": errors, "total_source_files": staged + skipped + len(errors)}


def processed_destination(link: ResearchWorkspaceBooking | None) -> dict[str, Any] | None:
    root, _, processed = find_folders(link)
    if link is None:
        return None
    root_name = root.name if root is not None else ""
    return {
        "workspace_id": str(link.workspace_id),
        "folder_id": str(processed.pk) if processed is not None else None,
        "path_label": " / ".join(p for p in (link.workspace.name, root_name, PROCESSED_FOLDER) if p),
    }


def output_files(workspace, *, paths: set[str] | None = None):
    """Current portal copies of collected Output (``Processed/...``), optionally limited to plan paths."""
    from iic_booking.remote_analysis.workspace_models import WorkspaceFile

    qs = WorkspaceFile.objects.filter(
        workspace=workspace, deleted=False, is_current=True, relative_path__startswith=f"{BRIDGE_PORTAL_FOLDER}/"
    ).order_by("relative_path")
    if paths is not None:
        qs = qs.filter(relative_path__in=[f"{BRIDGE_PORTAL_FOLDER}/{p}" for p in paths])
    return qs


def bridge_processed(
    workspace,
    link: ResearchWorkspaceBooking,
    *,
    paths: set[str] | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """Copy verified collected Output into ``<VID>/Processed Data``; idempotent by checksum, clashes -> "name (2).ext"."""
    from iic_booking.remote_analysis.workspace.storage import StorageManager

    actor = workspace.user
    booking = workspace.booking or getattr(workspace.reservation, "booking", None)
    research_ws = link.workspace
    _, _, processed = ensure_folders(link, booking, actor)
    files = list(output_files(workspace, paths=paths))
    state = {
        "files_total": len(files),
        "files_done": 0,
        "bytes_total": sum(int(f.size or 0) for f in files),
        "bytes_done": 0,
        "current_file": "",
    }
    storage_mgr = StorageManager()
    bridged: list[dict[str, str]] = []
    copied = skipped = 0
    errors: list[str] = []
    for wf in files:
        rel = wf.relative_path.split("/", 1)[1]
        dir_parts, leaf = _split_relative(rel)
        state["current_file"] = leaf
        if progress:
            progress(dict(state))
        checksum = hex_to_b64(wf.sha256)
        try:
            folder = _subfolder(research_ws, processed, dir_parts, actor)
            if checksum and ResearchFile.objects.filter(
                workspace=research_ws,
                folder=folder,
                status=FileStatus.AVAILABLE,
                origin=FileOrigin.ANALYSIS_OUTPUT,
                original_name=leaf[:255],
                checksum_sha256=checksum,
            ).exists():
                skipped += 1
            else:
                over = quota_error(research_ws, int(wf.size or 0))
                if over:
                    raise ResearchCopyError("quota", over)
                research_file = _new_research_file(
                    research_ws, folder, leaf, booking=booking, actor=actor, origin=FileOrigin.ANALYSIS_OUTPUT, size=int(wf.size or 0)
                )
                path = storage_mgr.read_file(workspace, wf.storage_relpath)
                with open(path, "rb") as fh:
                    verified = upload_verified(fh, research_file, checksum)
                _finalize_research_file(research_file, checksum_b64=checksum, verified=verified, actor=actor, any_type=True)
                copied += 1
            bridged.append({"path": rel, "sha256": wf.sha256, "file_id": str(wf.pk)})
        except ResearchCopyError as exc:
            errors.append(exc.detail)
            if exc.code == "quota":
                break
        except Exception as exc:  # noqa: BLE001
            logger.exception("My Research output bridge failed for %s", rel)
            errors.append(f"{rel}: {exc}")
        state["files_done"] += 1
        state["bytes_done"] += int(wf.size or 0)
    state["current_file"] = ""
    if progress:
        progress(dict(state))
    return {"copied": copied, "skipped": skipped, "errors": errors, "bridged": bridged, "total": len(files)}


def purge_bridged_local(workspace, bridged: list[dict[str, str]]) -> int:
    """Drop portal-disk copies of output already safe in My Research (same checksum)."""
    from iic_booking.remote_analysis.workspace.storage import StorageManager
    from iic_booking.remote_analysis.workspace_models import WorkspaceFile

    storage_mgr = StorageManager()
    purged = 0
    for item in bridged:
        wf = WorkspaceFile.objects.filter(pk=item.get("file_id"), workspace=workspace, deleted=False).first()
        if wf is None or (wf.sha256 or "").lower() != (item.get("sha256") or "").lower():
            continue
        try:
            path = storage_mgr.read_file(workspace, wf.storage_relpath)
            if path.exists():
                path.unlink()
        except Exception:  # noqa: BLE001
            logger.warning("Local purge failed for %s", wf.relative_path, exc_info=True)
            continue
        wf.deleted = True
        wf.is_current = False
        wf.save(update_fields=["deleted", "is_current", "modified_at"])
        purged += 1
    if purged:
        try:
            storage_mgr.recalculate_usage(workspace)
        except Exception:  # noqa: BLE001
            pass
    return purged
