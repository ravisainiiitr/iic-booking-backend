"""StorageManager — create/archive/restore workspaces on Portal local storage."""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import uuid
from datetime import timedelta
from pathlib import Path
from typing import BinaryIO

from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    DEFAULT_WORKSPACE_FOLDERS,
    ArchiveStatus,
    FileCategory,
    WorkspaceStatus,
)
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.workspace.audit import audit_workspace, record_metric
from iic_booking.remote_analysis.workspace_models import (
    AnalysisWorkspace,
    WorkspaceArchive,
    WorkspaceFolder,
    WorkspaceQuota,
)

logger = logging.getLogger(__name__)

FOLDER_CATEGORY = {
    "RawData": FileCategory.RAW,
    "Processed": FileCategory.PROCESSED,
    "Reports": FileCategory.REPORT,
    "Exports": FileCategory.EXPORT,
    "Temp": FileCategory.TEMP,
    "Logs": FileCategory.LOG,
    "Metadata": FileCategory.METADATA,
}


class StorageError(Exception):
    pass


class StorageManager:
    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()

    def workspace_root(self) -> Path:
        root = (self.settings.workspace_root or "").strip()
        if root:
            path = Path(root)
        else:
            media = Path(getattr(django_settings, "MEDIA_ROOT", ".") or ".")
            path = media / "remote_analysis" / "workspaces"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def archive_root(self) -> Path:
        root = (self.settings.archive_root or "").strip()
        if root:
            path = Path(root)
        else:
            media = Path(getattr(django_settings, "MEDIA_ROOT", ".") or ".")
            path = media / "remote_analysis" / "archives"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def absolute_path(self, workspace: AnalysisWorkspace, *parts: str) -> Path:
        base = self.workspace_root() / workspace.storage_key
        target = base.joinpath(*parts) if parts else base
        # Isolation: never escape workspace root
        resolved = target.resolve()
        if not str(resolved).startswith(str(base.resolve())):
            raise StorageError("Path escapes workspace boundary")
        return resolved

    def folder_names(self) -> list[str]:
        template = self.settings.folder_template
        if isinstance(template, list) and template:
            return [str(x) for x in template]
        return list(DEFAULT_WORKSPACE_FOLDERS)

    @transaction.atomic
    def create_for_reservation(
        self,
        reservation: AnalysisReservation,
        *,
        actor=None,
        quota_gb: float | None = None,
    ) -> AnalysisWorkspace:
        existing = AnalysisWorkspace.objects.filter(reservation=reservation).first()
        if existing and existing.status != WorkspaceStatus.DELETED:
            return existing

        quota = float(quota_gb if quota_gb is not None else self.settings.default_quota_gb)
        retention_days = int(self.settings.retention_days or 90)
        storage_key = f"ws-{reservation.id.hex[:16]}-{uuid.uuid4().hex[:8]}"

        workspace = AnalysisWorkspace.objects.create(
            reservation=reservation,
            booking=reservation.booking,
            user=reservation.user,
            department=reservation.department,
            workstation=reservation.workstation,
            status=WorkspaceStatus.CREATING,
            quota_gb=quota,
            retention_until=timezone.now() + timedelta(days=retention_days),
            storage_key=storage_key,
            local_agent_path=f"workspaces\\{storage_key}",
        )

        root = self.absolute_path(workspace)
        root.mkdir(parents=True, exist_ok=True)

        for name in self.folder_names():
            folder_path = root / name
            folder_path.mkdir(parents=True, exist_ok=True)
            read_only = name in {"Metadata"}
            WorkspaceFolder.objects.create(
                workspace=workspace,
                name=name,
                relative_path=name,
                read_only=read_only,
                category=FOLDER_CATEGORY.get(name, FileCategory.OTHER),
            )

        WorkspaceQuota.objects.create(
            workspace=workspace,
            user=reservation.user,
            department=reservation.department,
            soft_limit_bytes=int(quota * 0.8 * (1024**3)),
            hard_limit_bytes=int(quota * (1024**3)),
        )

        workspace.status = WorkspaceStatus.READY
        workspace.activated_at = timezone.now()
        workspace.save(update_fields=["status", "activated_at", "updated_at"])
        audit_workspace(workspace, "CREATE", details=storage_key, actor=actor)
        record_metric("workspace_created", 1, workspace=workspace)
        return workspace

    def recalculate_usage(self, workspace: AnalysisWorkspace) -> int:
        total = 0
        root = self.absolute_path(workspace)
        if root.exists():
            for dirpath, _, filenames in os.walk(root):
                for name in filenames:
                    try:
                        total += (Path(dirpath) / name).stat().st_size
                    except OSError:
                        continue
        workspace.current_usage_bytes = total
        workspace.save(update_fields=["current_usage_bytes", "updated_at"])
        return total

    def check_quota(self, workspace: AnalysisWorkspace, additional_bytes: int = 0, *, override: bool = False) -> None:
        usage = workspace.current_usage_bytes + max(0, additional_bytes)
        hard = int(workspace.quota_gb * (1024**3))
        quota = getattr(workspace, "quota", None)
        if quota and quota.hard_limit_bytes:
            hard = quota.hard_limit_bytes
            if override and quota.override_allowed:
                return
        if usage > hard:
            audit_workspace(workspace, "QUOTA", details=f"hard limit exceeded ({usage}>{hard})", success=False)
            raise StorageError("Workspace quota exceeded")

    def write_bytes(self, workspace: AnalysisWorkspace, relative_path: str, data: bytes | BinaryIO) -> tuple[Path, str, int]:
        """Write content under workspace; returns (path, sha256, size)."""
        rel = relative_path.replace("\\", "/").lstrip("/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if any(p == ".." for p in parts):
            raise StorageError("Invalid relative path")
        path = self.absolute_path(workspace, *parts)
        path.parent.mkdir(parents=True, exist_ok=True)
        hasher = hashlib.sha256()
        size = 0
        with open(path, "wb") as fh:
            if isinstance(data, (bytes, bytearray)):
                fh.write(data)
                hasher.update(data)
                size = len(data)
            else:
                while True:
                    chunk = data.read(1024 * 1024)
                    if not chunk:
                        break
                    fh.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
        return path, hasher.hexdigest(), size

    def read_file(self, workspace: AnalysisWorkspace, storage_relpath: str) -> Path:
        return self.absolute_path(workspace, *storage_relpath.replace("\\", "/").split("/"))

    def sha256_file(self, path: Path) -> str:
        hasher = hashlib.sha256()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1024 * 1024)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    def archive(self, workspace: AnalysisWorkspace, *, actor=None, note: str = "") -> WorkspaceArchive:
        import time

        t0 = time.time()
        workspace.status = WorkspaceStatus.ARCHIVING
        workspace.archive_status = ArchiveStatus.PENDING
        workspace.save(update_fields=["status", "archive_status", "updated_at"])

        src = self.absolute_path(workspace)
        archive_key = f"{workspace.storage_key}-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        dest_base = self.archive_root() / archive_key
        if not src.exists():
            raise StorageError("Workspace storage missing")

        shutil.make_archive(str(dest_base), "zip", root_dir=str(src))
        zip_path = Path(str(dest_base) + ".zip")
        digest = self.sha256_file(zip_path)
        size = zip_path.stat().st_size

        row = WorkspaceArchive.objects.create(
            workspace=workspace,
            archive_key=archive_key + ".zip",
            size_bytes=size,
            sha256=digest,
            created_by=actor if actor is not None and getattr(actor, "pk", None) else None,
            note=note[:512],
        )
        workspace.status = WorkspaceStatus.ARCHIVED
        workspace.archive_status = ArchiveStatus.ARCHIVED
        workspace.archived_at = timezone.now()
        workspace.read_only = True
        workspace.save(
            update_fields=["status", "archive_status", "archived_at", "read_only", "updated_at"]
        )
        audit_workspace(workspace, "ARCHIVE", details=row.archive_key, actor=actor)
        record_metric("archive_time_ms", (time.time() - t0) * 1000, unit="ms", workspace=workspace)
        record_metric("workspace_size_bytes", float(workspace.current_usage_bytes), unit="bytes", workspace=workspace)
        return row

    def restore(self, workspace: AnalysisWorkspace, archive: WorkspaceArchive | None = None, *, actor=None) -> AnalysisWorkspace:
        import time

        t0 = time.time()
        archive = archive or workspace.archives.order_by("-created_at").first()
        if not archive:
            raise StorageError("No archive available")

        workspace.status = WorkspaceStatus.RESTORING
        workspace.save(update_fields=["status", "updated_at"])

        zip_path = self.archive_root() / archive.archive_key
        if not zip_path.exists():
            raise StorageError("Archive file missing")

        expected = archive.sha256
        actual = self.sha256_file(zip_path)
        if expected and actual != expected:
            audit_workspace(workspace, "INTEGRITY", details="archive checksum mismatch", actor=actor, success=False)
            raise StorageError("Archive integrity check failed")

        dest = self.absolute_path(workspace)
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.unpack_archive(str(zip_path), extract_dir=str(dest))

        archive.restored_at = timezone.now()
        archive.save(update_fields=["restored_at"])
        workspace.status = WorkspaceStatus.READY
        workspace.archive_status = ArchiveStatus.RESTORED
        workspace.read_only = False
        workspace.save(update_fields=["status", "archive_status", "read_only", "updated_at"])
        self.recalculate_usage(workspace)
        audit_workspace(workspace, "RESTORE", details=archive.archive_key, actor=actor)
        record_metric("restore_time_ms", (time.time() - t0) * 1000, unit="ms", workspace=workspace)
        return workspace

    def delete_storage(self, workspace: AnalysisWorkspace) -> None:
        path = self.absolute_path(workspace)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        workspace.status = WorkspaceStatus.DELETED
        workspace.save(update_fields=["status", "updated_at"])
