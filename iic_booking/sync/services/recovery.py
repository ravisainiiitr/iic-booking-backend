"""Recovery facade for Department Sync Agent offline / DR (Milestone 13)."""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone

from iic_booking.sync.models import AgentConflictResolution, AgentRecoveryEvent, DepartmentSyncAgent
from iic_booking.sync.services.conflict_resolution import ConflictResolutionService
from iic_booking.sync.services.database_integrity import DatabaseIntegrityService
from iic_booking.sync.services.logging import write_sync_log


class RecoveryService:
    """Portal-side reconciliation and recovery event ingestion."""

    def __init__(self) -> None:
        self.conflicts = ConflictResolutionService()
        self.integrity = DatabaseIntegrityService()

    def record_event(
        self,
        sync_agent: DepartmentSyncAgent,
        *,
        event_code: str,
        message: str,
        component: str = "",
        from_state: str = "",
        to_state: str = "",
        correlation_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = AgentRecoveryEvent.objects.create(
            sync_agent=sync_agent,
            event_code=(event_code or "REC-0000")[:32],
            component=(component or "")[:64],
            from_state=(from_state or "")[:32],
            to_state=(to_state or "")[:32],
            message=(message or "")[:500],
            device_id=device_id or sync_agent.device_id,
            agent_uuid=sync_agent.agent_uuid,
            correlation_id=correlation_id,
            details=details or {},
        )
        write_sync_log(
            event_code=event_code or "REC-0000",
            message=f"{event_code}: {message}",
            category="recovery",
            sync_agent=sync_agent,
            correlation_id=correlation_id,
            json_payload={"component": component, "from": from_state, "to": to_state},
        )
        return {
            "id": event.id,
            "event_code": event.event_code,
            "created_at": event.created_at.isoformat(),
        }

    def reconcile(
        self,
        sync_agent: DepartmentSyncAgent,
        payload: dict[str, Any],
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Acknowledge agent catch-up after reconnect; never invent duplicate work."""
        offline_seconds = int(payload.get("offline_duration_seconds") or 0)
        pending_uploads = int(payload.get("pending_uploads") or 0)
        pending_processing = int(payload.get("pending_processing") or 0)
        repaired = int(payload.get("queue_repairs") or 0)
        conflicts = payload.get("conflicts") or []

        resolved = []
        for item in conflicts:
            if isinstance(item, dict):
                resolved.append(
                    self.conflicts.resolve(
                        sync_agent,
                        conflict_type=str(item.get("conflict_type") or "unknown"),
                        resolution=str(item.get("resolution") or "keep_existing"),
                        upload_id=item.get("upload_id"),
                        processing_id=item.get("processing_id"),
                        correlation_id=correlation_id,
                        details=item.get("details") or {},
                    )
                )

        self.record_event(
            sync_agent,
            event_code="REC-2001",
            message="Automatic reconciliation acknowledged",
            component="reconciliation",
            from_state="RECOVERING",
            to_state="ONLINE",
            correlation_id=correlation_id,
            details={
                "offline_duration_seconds": offline_seconds,
                "pending_uploads": pending_uploads,
                "pending_processing": pending_processing,
                "queue_repairs": repaired,
                "conflicts_resolved": len(resolved),
            },
        )

        sync_agent.last_seen_at = timezone.now()
        sync_agent.save(update_fields=["last_seen_at"])

        return {
            "status": "reconciled",
            "accepted_at": timezone.now().isoformat(),
            "conflicts": resolved,
            "duplicate_policy": "idempotent_by_upload_id",
        }

    def status(self, sync_agent: DepartmentSyncAgent) -> dict[str, Any]:
        recent = list(
            AgentRecoveryEvent.objects.filter(sync_agent=sync_agent)
            .order_by("-created_at")[:20]
            .values(
                "event_code",
                "component",
                "from_state",
                "to_state",
                "message",
                "correlation_id",
                "created_at",
            )
        )
        conflict_count = AgentConflictResolution.objects.filter(sync_agent=sync_agent).count()
        return {
            "agent_uuid": str(sync_agent.agent_uuid),
            "device_id": str(sync_agent.device_id) if sync_agent.device_id else None,
            "recent_events": recent,
            "conflict_count": conflict_count,
        }
