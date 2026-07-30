"""Analysis workspace analysis.json metadata (recovery / ops)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.workspace.storage import StorageManager

logger = logging.getLogger(__name__)

METADATA_FILENAME = "analysis.json"


class AnalysisWorkspaceMetadata:
    """Read/write Metadata/analysis.json for Analysis Jobs.

    Database job state is authoritative. analysis.json is auxiliary — write
    failures must never roll back job transactions (R3).
    """

    def __init__(self, storage: StorageManager | None = None):
        self.storage = storage or StorageManager()

    def path_for(self, workspace) -> Path:
        return self.storage.absolute_path(workspace, "Metadata", METADATA_FILENAME)

    def build_payload(self, job, *, extra: dict | None = None) -> dict[str, Any]:
        wf = job.workflow_version.workflow
        version = job.workflow_version
        active = job.steps.filter(step_number=job.current_step_number).first()
        ws = None
        if active and active.workstation_id:
            ws = active.workstation
        elif job.preferred_workstation_id:
            ws = job.preferred_workstation
        payload: dict[str, Any] = {
            "workflow": wf.name,
            "workflow_slug": wf.slug,
            "version": version.label or f"{version.version_number}",
            "version_number": version.version_number,
            "job_id": str(job.id),
            "currentStep": job.current_step_number,
            "booking": getattr(job.booking, "virtual_booking_id", None)
            or getattr(job.booking, "booking_id", None)
            or str(job.booking_id),
            # Ops recovery only (file on disk, not returned on user APIs)
            "allocatedPC": (ws.hostname if ws is not None else ""),
            "allocatedEnvironment": (active.environment_label if active else ""),
            "startedBy": getattr(job.owner, "email", None) or str(job.owner_id),
            "status": job.status,
            "uxStatus": job.ux_status or "",
            "sameEnvironment": bool(job.same_environment),
            "metadataStatus": "ok",
            "updatedAt": timezone.now().isoformat(),
        }
        if extra:
            payload.update(extra)
        return payload

    def write(self, job, *, extra: dict | None = None) -> Path | None:
        """Best-effort metadata write. Never raises — DB remains source of truth."""
        if not job.workspace_id:
            return None
        try:
            workspace = job.workspace
            self.storage.ensure_folders(workspace, ["Metadata"])
            path = self.path_for(workspace)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = self.build_payload(job, extra=extra)
            path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
            self._mark_metadata_ok(job)
            return path
        except Exception as exc:  # noqa: BLE001 — must not abort Analysis Job transactions
            logger.exception(
                "analysis.json write failed for job=%s workspace=%s: %s",
                getattr(job, "id", None),
                getattr(job, "workspace_id", None),
                exc,
            )
            self._mark_metadata_stale(job, str(exc))
            return None

    def _mark_metadata_ok(self, job) -> None:
        detail = (job.status_detail or "").strip()
        if "metadata_stale:" not in detail:
            return
        cleaned = "\n".join(
            line for line in detail.splitlines() if not line.startswith("metadata_stale:")
        ).strip()
        try:
            from iic_booking.remote_analysis.workflow_models import AnalysisJob

            AnalysisJob.objects.filter(pk=job.pk).update(
                status_detail=cleaned, updated_at=timezone.now()
            )
            job.status_detail = cleaned
        except Exception:  # noqa: BLE001
            logger.warning("Could not clear metadata_stale flag on job %s", job.id)

    def _mark_metadata_stale(self, job, reason: str) -> None:
        """Record auxiliary metadata failure without changing job status."""
        note = f"metadata_stale: {reason[:200]}"
        detail = (job.status_detail or "").strip()
        if note in detail:
            return
        new_detail = f"{detail}\n{note}".strip() if detail else note
        try:
            from iic_booking.remote_analysis.workflow_models import AnalysisJob

            AnalysisJob.objects.filter(pk=job.pk).update(
                status_detail=new_detail, updated_at=timezone.now()
            )
            job.status_detail = new_detail
        except Exception:  # noqa: BLE001
            logger.warning("Could not record metadata_stale on job %s", job.id)

    def read(self, workspace) -> dict[str, Any]:
        path = self.path_for(workspace)
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to read analysis.json: %s", exc)
            return {}
