"""TransferManager — Portal uploads/downloads with integrity, versioning, quotas."""

from __future__ import annotations

import io
import logging
import mimetypes
import time
import uuid
import zipfile
from pathlib import Path
from typing import BinaryIO, Iterable

from django.db import transaction
from django.http import FileResponse, HttpResponse
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    FileCategory,
    TransferDirection,
    TransferStatus,
    VirusStatus,
    WorkspaceStatus,
)
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.workspace.audit import audit_workspace, record_metric
from iic_booking.remote_analysis.workspace.scanner import scan_and_record
from iic_booking.remote_analysis.workspace.storage import StorageError, StorageManager
from iic_booking.remote_analysis.workspace_models import (
    AnalysisWorkspace,
    TransferHistory,
    TransferPolicy,
    WorkspaceFile,
    WorkspaceFolder,
    WorkspaceTransfer,
    WorkspaceVersion,
)

logger = logging.getLogger(__name__)


class TransferError(Exception):
    def __init__(self, message: str, *, code: str = "transfer_error"):
        super().__init__(message)
        self.code = code


class TransferManager:
    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()
        self.storage = StorageManager(self.settings)

    def _extension_allowed(self, filename: str, workspace: AnalysisWorkspace) -> None:
        ext = Path(filename).suffix.lower()
        blocked = {
            e.strip().lower()
            for e in (self.settings.blocked_extensions or "").split(",")
            if e.strip()
        }
        allowed_raw = (self.settings.allowed_extensions or "").strip()
        policy = (
            TransferPolicy.objects.filter(is_active=True, workstation=workspace.workstation).first()
            or TransferPolicy.objects.filter(is_active=True, department=workspace.department).first()
        )
        if policy:
            if policy.blocked_extensions:
                blocked |= {e.strip().lower() for e in policy.blocked_extensions.split(",") if e.strip()}
            if policy.allowed_extensions:
                allowed_raw = policy.allowed_extensions
            if policy.max_file_size:
                # checked later with size
                pass

        if ext and ext in blocked:
            raise TransferError(f"Extension {ext} is blocked", code="blocked_extension")
        if allowed_raw:
            allowed = {e.strip().lower() for e in allowed_raw.split(",") if e.strip()}
            if ext and ext not in allowed:
                raise TransferError(f"Extension {ext} is not allowed", code="extension_not_allowed")

    def _folder_writable(self, workspace: AnalysisWorkspace, relative_folder: str) -> WorkspaceFolder | None:
        folder = WorkspaceFolder.objects.filter(workspace=workspace, relative_path=relative_folder).first()
        if folder and folder.read_only:
            raise TransferError("Folder is read-only", code="read_only_folder")
        if workspace.read_only:
            raise TransferError("Workspace is read-only", code="workspace_read_only")
        return folder

    def _category_for_folder(self, folder_name: str) -> str:
        mapping = {
            "RawData": FileCategory.RAW,
            "Processed": FileCategory.PROCESSED,
            "Reports": FileCategory.REPORT,
            "Exports": FileCategory.EXPORT,
            "Temp": FileCategory.TEMP,
            "Logs": FileCategory.LOG,
            "Metadata": FileCategory.METADATA,
        }
        return mapping.get(folder_name, FileCategory.OTHER)

    @transaction.atomic
    def upload(
        self,
        workspace: AnalysisWorkspace,
        uploaded_file,
        *,
        folder: str = "RawData",
        actor=None,
        expected_sha256: str = "",
        source: str = "portal",
        override_quota: bool = False,
        relative_name: str = "",
    ) -> WorkspaceFile:
        if workspace.status in {WorkspaceStatus.ARCHIVED, WorkspaceStatus.DELETED}:
            raise TransferError("Workspace not writable", code="workspace_closed")

        original_name = getattr(uploaded_file, "name", "upload.bin") or "upload.bin"
        # Allow nested relative paths from agent (Output/a/b.csv → Processed/a/b.csv)
        if relative_name:
            safe_rel = relative_name.replace("\\", "/").lstrip("/")
            if ".." in safe_rel.split("/"):
                raise TransferError("Invalid relative path", code="path_traversal")
            original_name = safe_rel
        self._extension_allowed(Path(original_name).name, workspace)
        folder = (folder or "RawData").replace("\\", "/").strip("/")
        ws_folder = self._folder_writable(workspace, folder.split("/")[0])

        # Size check
        size_hint = getattr(uploaded_file, "size", None) or 0
        if size_hint and size_hint > self.settings.maximum_upload_size:
            raise TransferError("File exceeds maximum upload size", code="too_large")
        policy = TransferPolicy.objects.filter(is_active=True, workstation=workspace.workstation).first()
        if policy and policy.max_file_size and size_hint and size_hint > policy.max_file_size:
            raise TransferError("File exceeds policy max size", code="policy_size")

        relative_path = f"{folder}/{original_name}".replace("//", "/")
        # Manifest resume: skip rewrite when current file already matches expected sha256
        if expected_sha256:
            existing_match = (
                WorkspaceFile.objects.filter(
                    workspace=workspace,
                    relative_path=relative_path,
                    deleted=False,
                    is_current=True,
                    sha256__iexact=expected_sha256,
                )
                .first()
            )
            if existing_match:
                transfer = WorkspaceTransfer.objects.create(
                    workspace=workspace,
                    file=existing_match,
                    direction=TransferDirection.PORTAL_TO_WORKSPACE
                    if source == "portal"
                    else TransferDirection.AGENT_PUSH,
                    status=TransferStatus.COMPLETED,
                    bytes_total=existing_match.size,
                    bytes_transferred=existing_match.size,
                    checksum_expected=expected_sha256,
                    checksum_actual=existing_match.sha256,
                    created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
                    started_at=timezone.now(),
                    completed_at=timezone.now(),
                )
                TransferHistory.objects.create(transfer=transfer, event="skipped", detail="checksum_match")
                existing_match._skipped_unchanged = True  # type: ignore[attr-defined]
                return existing_match

        self.storage.check_quota(workspace, size_hint or 0, override=override_quota)

        transfer = WorkspaceTransfer.objects.create(
            workspace=workspace,
            direction=TransferDirection.PORTAL_TO_WORKSPACE if source == "portal" else TransferDirection.AGENT_PUSH,
            status=TransferStatus.IN_PROGRESS,
            bytes_total=size_hint or 0,
            chunk_size=self.settings.chunk_size_bytes,
            checksum_expected=expected_sha256 or "",
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
            started_at=timezone.now(),
        )
        TransferHistory.objects.create(transfer=transfer, event="started", detail=original_name)

        t0 = time.time()
        stored_name = f"{uuid.uuid4().hex}_{Path(original_name).name}"
        storage_relpath = f"{folder}/{stored_name}"

        try:
            path, digest, size = self.storage.write_bytes(workspace, storage_relpath, uploaded_file)
        except Exception as exc:
            transfer.status = TransferStatus.FAILED
            transfer.error_message = str(exc)[:2000]
            transfer.completed_at = timezone.now()
            transfer.save(update_fields=["status", "error_message", "completed_at"])
            TransferHistory.objects.create(transfer=transfer, event="failed", detail=str(exc)[:512])
            record_metric("transfer_failures", 1, workspace=workspace, tags={"direction": "upload"})
            raise TransferError(str(exc), code="write_failed") from exc

        if expected_sha256 and expected_sha256.lower() != digest.lower():
            path.unlink(missing_ok=True)
            transfer.status = TransferStatus.FAILED
            transfer.checksum_actual = digest
            transfer.error_message = "Checksum mismatch"
            transfer.completed_at = timezone.now()
            transfer.save(update_fields=["status", "checksum_actual", "error_message", "completed_at"])
            audit_workspace(workspace, "INTEGRITY", details=original_name, actor=actor, success=False)
            record_metric("checksum_failures", 1, workspace=workspace)
            raise TransferError("Checksum verification failed", code="checksum_mismatch")

        self.storage.check_quota(workspace, size, override=override_quota)

        existing = (
            WorkspaceFile.objects.select_for_update()
            .filter(workspace=workspace, relative_path=relative_path, is_current=True, deleted=False)
            .first()
        )
        mime, _ = mimetypes.guess_type(original_name)

        if existing:
            # Snapshot current content under its version (idempotent if already recorded)
            WorkspaceVersion.objects.update_or_create(
                file=existing,
                version=existing.version,
                defaults={
                    "size": existing.size,
                    "sha256": existing.sha256,
                    "storage_relpath": existing.storage_relpath,
                    "created_by": actor if actor is not None and getattr(actor, "pk", None) else None,
                    "note": "Superseded by upload",
                },
            )
            # Prune history
            limit = self.settings.version_history_limit or 20
            old_versions = list(existing.versions.order_by("-version")[limit:])
            for v in old_versions:
                try:
                    old_path = self.storage.read_file(workspace, v.storage_relpath)
                    if old_path.exists():
                        old_path.unlink()
                except Exception:
                    pass
                v.delete()

            existing.stored_name = stored_name
            existing.storage_relpath = storage_relpath
            existing.size = size
            existing.sha256 = digest
            existing.mime_type = mime or ""
            existing.version = existing.version + 1
            existing.virus_status = VirusStatus.PENDING
            existing.source = source
            existing.uploaded_by = actor if actor is not None and getattr(actor, "pk", None) else existing.uploaded_by
            existing.folder = ws_folder
            existing.save()
            file_row = existing
            audit_workspace(workspace, "VERSION", details=f"{relative_path} v{file_row.version}", actor=actor)
        else:
            file_row = WorkspaceFile.objects.create(
                workspace=workspace,
                folder=ws_folder,
                original_name=Path(original_name).name,
                stored_name=stored_name,
                relative_path=relative_path,
                size=size,
                sha256=digest,
                mime_type=mime or "",
                category=self._category_for_folder(folder.split("/")[0]),
                uploaded_by=actor if actor is not None and getattr(actor, "pk", None) else None,
                version=1,
                is_current=True,
                storage_relpath=storage_relpath,
                source=source,
            )
            WorkspaceVersion.objects.create(
                file=file_row,
                version=1,
                size=size,
                sha256=digest,
                storage_relpath=storage_relpath,
                created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
                note="Initial version",
            )

        scan_and_record(file_row, str(path), settings_obj=self.settings)
        if file_row.virus_status == VirusStatus.INFECTED:
            path.unlink(missing_ok=True)
            file_row.deleted = True
            file_row.save(update_fields=["deleted", "modified_at"])
            raise TransferError("File failed virus scan", code="virus_infected")

        self.storage.recalculate_usage(workspace)

        elapsed = max(0.001, time.time() - t0)
        transfer.file = file_row
        transfer.status = TransferStatus.COMPLETED
        transfer.bytes_total = size
        transfer.bytes_transferred = size
        transfer.checksum_actual = digest
        transfer.completed_at = timezone.now()
        transfer.save()
        TransferHistory.objects.create(transfer=transfer, event="completed", detail=digest[:16])
        audit_workspace(workspace, "UPLOAD", details=relative_path, actor=actor)
        record_metric("upload_speed_bps", size / elapsed, unit="bps", workspace=workspace)
        record_metric("average_transfer_time_ms", elapsed * 1000, unit="ms", workspace=workspace)
        try:
            from iic_booking.remote_analysis.collaboration.hooks import on_transfer_complete

            on_transfer_complete(transfer, is_upload=True)
        except Exception:
            pass
        return file_row

    def download_file(self, workspace: AnalysisWorkspace, file: WorkspaceFile, *, actor=None) -> FileResponse:
        if file.deleted or not file.is_current:
            raise TransferError("File not available", code="not_found")
        if file.size > self.settings.maximum_download_size:
            raise TransferError("File exceeds maximum download size", code="too_large")

        path = self.storage.read_file(workspace, file.storage_relpath)
        if not path.exists():
            raise TransferError("Stored content missing", code="missing")

        actual = self.storage.sha256_file(path)
        if file.sha256 and actual != file.sha256:
            audit_workspace(workspace, "INTEGRITY", details=str(file.id), actor=actor, success=False)
            record_metric("checksum_failures", 1, workspace=workspace)
            raise TransferError("Integrity check failed", code="checksum_mismatch")

        transfer = WorkspaceTransfer.objects.create(
            workspace=workspace,
            file=file,
            direction=TransferDirection.WORKSPACE_TO_PORTAL,
            status=TransferStatus.COMPLETED,
            bytes_total=file.size,
            bytes_transferred=file.size,
            checksum_expected=file.sha256,
            checksum_actual=actual,
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
            started_at=timezone.now(),
            completed_at=timezone.now(),
        )
        TransferHistory.objects.create(transfer=transfer, event="download", detail=file.original_name)
        file.download_count += 1
        file.save(update_fields=["download_count", "modified_at"])
        audit_workspace(workspace, "DOWNLOAD", details=file.relative_path, actor=actor)
        record_metric("download_speed_bps", float(file.size), unit="bytes", workspace=workspace)
        try:
            from iic_booking.remote_analysis.collaboration.hooks import on_transfer_complete

            on_transfer_complete(transfer, is_upload=False)
        except Exception:
            pass

        return FileResponse(
            open(path, "rb"),
            as_attachment=True,
            filename=file.original_name,
            content_type=file.mime_type or "application/octet-stream",
        )

    def download_zip(self, workspace: AnalysisWorkspace, files: Iterable[WorkspaceFile], *, actor=None) -> HttpResponse:
        buffer = io.BytesIO()
        count = 0
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in files:
                if file.deleted:
                    continue
                path = self.storage.read_file(workspace, file.storage_relpath)
                if not path.exists():
                    continue
                zf.write(path, arcname=file.relative_path)
                count += 1
                file.download_count += 1
                file.save(update_fields=["download_count", "modified_at"])
        buffer.seek(0)
        audit_workspace(workspace, "DOWNLOAD", details=f"zip:{count} files", actor=actor)
        response = HttpResponse(buffer.getvalue(), content_type="application/zip")
        response["Content-Disposition"] = f'attachment; filename="workspace-{workspace.id}.zip"'
        return response

    def soft_delete(self, workspace: AnalysisWorkspace, file: WorkspaceFile, *, actor=None) -> None:
        if workspace.read_only:
            raise TransferError("Workspace is read-only", code="workspace_read_only")
        file.deleted = True
        file.is_current = False
        file.save(update_fields=["deleted", "is_current", "modified_at"])
        audit_workspace(workspace, "DELETE", details=file.relative_path, actor=actor)
        self.storage.recalculate_usage(workspace)
