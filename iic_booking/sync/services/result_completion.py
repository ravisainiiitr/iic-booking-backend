"""Booking finalization after successful result import."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone as dt_timezone

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.models import Booking
from iic_booking.sync.exceptions import SyncControlPlaneError
from iic_booking.sync.models import (
    DepartmentSyncAgent,
    EquipmentResult,
    ResultProcessingQueue,
    ResultProcessingStatus,
)
from iic_booking.sync.services.result_notifications import (
    EVENT_RESULT_FINALIZED,
    ResultNotificationHooks,
)


class ResultCompletionError(SyncControlPlaneError):
    code = "RESULT_COMPLETION_FAILED"
    status_code = 400
    default_message = "Result completion failed."


class ResultCompletionService:
    def __init__(self) -> None:
        self._hooks = ResultNotificationHooks()

    @transaction.atomic
    def finalize(
        self,
        agent: DepartmentSyncAgent,
        *,
        agent_upload_id: uuid.UUID,
        booking_id: int,
        processing_duration_ms: int | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> dict:
        booking = Booking.objects.select_for_update().filter(pk=booking_id).first()
        if booking is None:
            raise ResultCompletionError("Booking not found.", code="BOOKING_MISSING")

        result = (
            EquipmentResult.objects.filter(sync_agent=agent, agent_upload_id=agent_upload_id)
            .select_related("processing_job")
            .first()
        )
        if result is None:
            raise ResultCompletionError("EquipmentResult not found for upload.", code="RESULT_MISSING")

        job = (
            ResultProcessingQueue.objects.select_for_update()
            .filter(sync_agent=agent, agent_upload_id=agent_upload_id)
            .first()
        )
        if job is not None:
            job.status = ResultProcessingStatus.FINALIZING_BOOKING
            job.started_at = job.started_at or timezone.now()
            job.save(update_fields=["status", "started_at", "updated_at"])

        # Transport finalization no longer changes booking lifecycle state.
        # Booking completion is handled by explicit manual completion or end-time
        # auto-completion logic based on stable result availability.

        if processing_duration_ms is not None:
            result.processing_duration_ms = processing_duration_ms
            result.processed_by = f"DepartmentSyncAgent:{agent.uuid}"
            result.save(update_fields=["processing_duration_ms", "processed_by", "updated_at"])

        if job is not None:
            job.status = ResultProcessingStatus.COMPLETED
            job.completed_at = timezone.now()
            job.error_message = ""
            job.version = (job.version or 0) + 1
            job.save(update_fields=["status", "completed_at", "error_message", "version", "updated_at"])

        attachment_ids = list(result.attachments.values_list("id", flat=True))
        self._hooks.audit(
            sync_agent=agent,
            event_code=EVENT_RESULT_FINALIZED,
            message="Booking finalized after result processing.",
            correlation_id=correlation_id,
            payload={
                "booking_id": booking.booking_id,
                "result_id": str(result.id),
                "attachments": [str(a) for a in attachment_ids],
                "completed_at": (booking.completed_at or timezone.now()).isoformat(),
            },
        )
        self._hooks.booking_activity(
            booking_id=booking.booking_id,
            message="Results processed; booking completed.",
            payload={"result_id": str(result.id)},
        )
        self._hooks.system_event(
            message="Result processing finalized.",
            payload={"agent_upload_id": str(agent_upload_id)},
        )

        return {
            "decision": "finalized",
            "booking_id": booking.booking_id,
            "booking_status": booking.status,
            "completed_at": (booking.completed_at or datetime.now(tz=dt_timezone.utc)).isoformat(),
            "result_id": str(result.id),
            "attachment_ids": [str(a) for a in attachment_ids],
            "processed_by": result.processed_by,
            "processing_duration_ms": result.processing_duration_ms,
        }
