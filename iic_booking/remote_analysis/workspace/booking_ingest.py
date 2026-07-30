"""Ingest booking / DSA result files into an Analysis Workspace (portal-side).

Reuses equipment.booking_results_service — does not reimplement DSA upload.
"""

from __future__ import annotations

import hashlib
import logging
import re
from io import BytesIO
from pathlib import Path
from typing import Any

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    TransferDirection,
    TransferStatus,
    WorkspaceStatus,
    WorkspaceSyncPhase,
)
from iic_booking.remote_analysis.workspace.audit import audit_workspace
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.remote_analysis.workspace_models import (
    AnalysisWorkspace,
    WorkspaceFile,
    WorkspaceTransfer,
)

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._\- ]+")


def _safe_filename(name: str) -> str:
    base = Path(name or "result.bin").name
    cleaned = _SAFE_NAME.sub("_", base).strip(" .") or "result.bin"
    return cleaned[:200]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_uploaded(name: str, data: bytes) -> InMemoryUploadedFile:
    buf = BytesIO(data)
    return InMemoryUploadedFile(
        file=buf,
        field_name="file",
        name=name,
        content_type="application/octet-stream",
        size=len(data),
        charset=None,
    )


class BookingResultIngestService:
    """Copy booking-owned experiment results into workspace RawData/."""

    def __init__(self, transfer: TransferManager | None = None):
        self.transfer = transfer or TransferManager()

    def ingest(self, workspace: AnalysisWorkspace, *, actor=None) -> dict[str, Any]:
        booking = workspace.booking or getattr(workspace.reservation, "booking", None)
        if booking is None:
            return {"ingested": 0, "skipped": 0, "failed": 0, "message": "No booking linked"}

        self._set_phase(workspace, WorkspaceSyncPhase.PREPARING, 5, "Seeding booking results")

        from iic_booking.equipment.booking_results_service import (
            iter_booking_result_zip_members,
            iter_dsa_zip_members,
        )

        ingested = skipped = failed = 0
        errors: list[str] = []

        # DSA attachments (local path or S3 bytes)
        for arcname, payload in iter_dsa_zip_members(booking):
            name = _safe_filename(Path(arcname).name)
            try:
                if isinstance(payload, (bytes, bytearray)):
                    data = bytes(payload)
                else:
                    data = Path(payload).read_bytes()
                result = self._ingest_bytes(workspace, name, data, actor=actor, source="dsa")
                if result == "ingested":
                    ingested += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{name}: {exc}")
                logger.exception("DSA ingest failed for workspace %s file %s", workspace.id, name)

        # Operator BookingResultFile uploads
        for arcname, file_field in iter_booking_result_zip_members(booking):
            name = _safe_filename(Path(arcname).name)
            try:
                with file_field.open("rb") as fh:
                    data = fh.read()
                result = self._ingest_bytes(workspace, name, data, actor=actor, source="booking_result")
                if result == "ingested":
                    ingested += 1
                else:
                    skipped += 1
            except Exception as exc:
                failed += 1
                errors.append(f"{name}: {exc}")
                logger.exception("BookingResultFile ingest failed for workspace %s file %s", workspace.id, name)

        msg = f"ingested={ingested} skipped={skipped} failed={failed}"
        audit_workspace(workspace, "UPLOAD", details=f"booking_ingest {msg}", actor=actor, success=failed == 0)
        if failed and ingested == 0 and skipped == 0:
            self._set_phase(workspace, WorkspaceSyncPhase.PREPARATION_FAILED, 0, errors[0] if errors else msg)
        else:
            self._set_phase(workspace, WorkspaceSyncPhase.PREPARING, 15, msg)
        return {"ingested": ingested, "skipped": skipped, "failed": failed, "errors": errors[:10], "message": msg}

    def _ingest_bytes(
        self,
        workspace: AnalysisWorkspace,
        name: str,
        data: bytes,
        *,
        actor=None,
        source: str,
    ) -> str:
        digest = _sha256_bytes(data)
        relative_path = f"RawData/{name}"
        existing = WorkspaceFile.objects.filter(
            workspace=workspace,
            relative_path=relative_path,
            deleted=False,
            is_current=True,
        ).first()
        if existing and existing.sha256.lower() == digest.lower():
            return "skipped"

        uploaded = _as_uploaded(name, data)
        try:
            self.transfer.upload(
                workspace,
                uploaded,
                folder="RawData",
                actor=actor,
                expected_sha256=digest,
                source=source,
                override_quota=True,
            )
        except TransferError as exc:
            if exc.code == "checksum_mismatch":
                raise
            # Extension policy may block some lab formats — record and continue
            raise
        return "ingested"

    @staticmethod
    def _set_phase(workspace: AnalysisWorkspace, phase: str, percent: int, message: str) -> None:
        workspace.sync_phase = phase
        workspace.sync_progress_percent = min(100, max(0, percent))
        workspace.sync_message = (message or "")[:512]
        workspace.save(update_fields=["sync_phase", "sync_progress_percent", "sync_message", "updated_at"])
