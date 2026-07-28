"""Deterministic conflict resolution for agent/portal catch-up (Milestone 13)."""

from __future__ import annotations

import uuid
from typing import Any

from iic_booking.sync.models import AgentConflictResolution, AgentUploadSession, DepartmentSyncAgent
from iic_booking.sync.services.logging import write_sync_log


class ConflictResolutionService:
    """Resolves duplicate upload/processing conflicts deterministically."""

    def resolve(
        self,
        sync_agent: DepartmentSyncAgent,
        *,
        conflict_type: str,
        resolution: str | None = None,
        upload_id: uuid.UUID | str | None = None,
        processing_id: uuid.UUID | str | None = None,
        correlation_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        conflict_type = (conflict_type or "unknown").lower()
        upload_uuid = _as_uuid(upload_id)
        processing_uuid = _as_uuid(processing_id)

        decided = resolution or self._decide(sync_agent, conflict_type, upload_uuid)
        record = AgentConflictResolution.objects.create(
            sync_agent=sync_agent,
            conflict_type=conflict_type[:64],
            resolution=decided[:64],
            upload_id=upload_uuid,
            processing_id=processing_uuid,
            correlation_id=correlation_id,
            details=details or {},
        )
        write_sync_log(
            event_code="REC-3001",
            message=f"Conflict {conflict_type} resolved as {decided}",
            category="recovery",
            sync_agent=sync_agent,
            correlation_id=correlation_id,
            json_payload={"upload_id": str(upload_uuid) if upload_uuid else None},
        )
        return {
            "id": str(record.id),
            "conflict_type": record.conflict_type,
            "resolution": record.resolution,
            "upload_id": str(upload_uuid) if upload_uuid else None,
        }

    def _decide(
        self,
        sync_agent: DepartmentSyncAgent,
        conflict_type: str,
        upload_id: uuid.UUID | None,
    ) -> str:
        if conflict_type in {"duplicate_upload", "portal_already_processed"}:
            if upload_id and AgentUploadSession.objects.filter(
                sync_agent=sync_agent, agent_upload_id=upload_id
            ).exists():
                return "keep_portal_session"
            return "keep_agent_queue"
        if conflict_type == "duplicate_processing":
            return "skip_duplicate_processing"
        if conflict_type == "missing_booking":
            return "defer_until_booking_synced"
        if conflict_type == "version_mismatch":
            return "prefer_portal_version"
        if conflict_type == "conflicting_queue_state":
            return "prefer_terminal_completed"
        return "keep_existing"


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
