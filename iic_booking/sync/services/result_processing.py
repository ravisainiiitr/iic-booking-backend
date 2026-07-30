"""Import parsed measurements into EquipmentResult records (idempotent)."""

from __future__ import annotations

import uuid
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.equipment.models import Booking, Equipment
from iic_booking.sync.exceptions import SyncControlPlaneError
from iic_booking.sync.models import (
    AgentUploadSession,
    DepartmentSyncAgent,
    EquipmentMeasurement,
    EquipmentResult,
    ResultAttachment,
    ResultProcessingQueue,
    ResultProcessingStatus,
)
from iic_booking.sync.services.result_notifications import (
    EVENT_RESULT_FAILED,
    EVENT_RESULT_IMPORTED,
    ResultNotificationHooks,
)
from iic_booking.sync.services.result_validation import (
    ATTACHMENT_ONLY_EXTENSIONS,
    normalize_extension,
    validate_import_payload,
)


def _publish_attachment_to_s3_and_cleanup(attachment_id, booking_id: int) -> None:
    """Upload local sync_uploads file to S3 Results/{vid}/ then delete the portal temp copy."""
    from iic_booking.equipment.booking_results_service import resolve_dsa_attachment_path
    from iic_booking.sync.services.results_s3 import (
        delete_local_upload_copy,
        upload_local_file_to_results_s3,
    )

    attachment = (
        ResultAttachment.objects.select_related("result__booking", "upload_session")
        .filter(id=attachment_id, result__booking_id=booking_id)
        .first()
    )
    if attachment is None:
        return
    if (attachment.s3_key or "").strip():
        return

    booking = attachment.result.booking
    virtual_id = (booking.virtual_booking_id or "").strip() or f"booking-{booking.pk}"
    local_path = resolve_dsa_attachment_path(attachment)
    if local_path is None:
        return

    s3_key = upload_local_file_to_results_s3(
        virtual_booking_id=virtual_id,
        local_path=local_path,
        file_name=attachment.file_name or local_path.name,
        content_type=attachment.content_type or "",
    )
    if not s3_key:
        return

    attachment.s3_key = s3_key
    # Keep storage_path for audit but mark local gone after delete.
    attachment.save(update_fields=["s3_key"])
    if delete_local_upload_copy(local_path):
        attachment.storage_path = ""
        attachment.save(update_fields=["storage_path"])


class ResultImportError(SyncControlPlaneError):
    code = "RESULT_IMPORT_FAILED"
    status_code = 400
    default_message = "Result import failed."


class ResultProcessingService:
    def __init__(self) -> None:
        self._hooks = ResultNotificationHooks()

    @transaction.atomic
    def import_results(
        self,
        agent: DepartmentSyncAgent,
        data: dict,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict:
        validate_import_payload(data)

        agent_upload_id = uuid.UUID(str(data["agent_upload_id"]))
        booking_id = int(data["booking_id"])
        equipment_id = int(data["equipment_id"])

        booking = Booking.objects.filter(pk=booking_id).first()
        if booking is None:
            raise ResultImportError("Booking not found.", code="BOOKING_MISSING")

        equipment = Equipment.objects.filter(pk=equipment_id).first()
        if equipment is None:
            raise ResultImportError("Equipment not found.", code="EQUIPMENT_MISSING")

        existing = EquipmentResult.objects.filter(
            sync_agent=agent,
            agent_upload_id=agent_upload_id,
        ).first()
        if existing is not None:
            return {
                "decision": "already_imported",
                "result_id": str(existing.id),
                "duplicate": True,
                "measurement_count": existing.measurements.count(),
                "attachment_count": existing.attachments.count(),
            }

        upload_session = None
        session_id = data.get("upload_session_id")
        if session_id:
            upload_session = AgentUploadSession.objects.filter(
                sync_agent=agent,
                id=uuid.UUID(str(session_id)),
            ).first()
        if upload_session is None:
            upload_session = AgentUploadSession.objects.filter(
                sync_agent=agent,
                agent_upload_id=agent_upload_id,
            ).first()

        job, _ = ResultProcessingQueue.objects.get_or_create(
            sync_agent=agent,
            agent_upload_id=agent_upload_id,
            defaults={
                "upload_session": upload_session,
                "booking": booking,
                "equipment": equipment,
                "status": ResultProcessingStatus.IMPORTING,
                "started_at": timezone.now(),
                "parser_used": data.get("parser_used") or "",
                "metadata": data.get("metadata") or {},
                "correlation_id": correlation_id,
            },
        )
        if job.status == ResultProcessingStatus.COMPLETED:
            existing = EquipmentResult.objects.filter(
                sync_agent=agent,
                agent_upload_id=agent_upload_id,
            ).first()
            if existing:
                return {
                    "decision": "already_imported",
                    "result_id": str(existing.id),
                    "duplicate": True,
                }

        job.status = ResultProcessingStatus.CREATING_RESULTS
        job.parser_used = data.get("parser_used") or job.parser_used
        job.metadata = data.get("metadata") or job.metadata
        job.booking = booking
        job.equipment = equipment
        job.upload_session = upload_session or job.upload_session
        job.started_at = job.started_at or timezone.now()
        job.save()

        result = EquipmentResult.objects.create(
            sync_agent=agent,
            booking=booking,
            equipment=equipment,
            upload_session=upload_session,
            processing_job=job,
            agent_upload_id=agent_upload_id,
            parser_used=data.get("parser_used") or "",
            source_file_name=data.get("file_name") or "",
            metadata=data.get("metadata") or {},
            correlation_id=correlation_id,
        )

        measurements = data.get("measurements") or []
        for row in measurements:
            if not isinstance(row, dict):
                continue
            ts = row.get("timestamp")
            parsed_ts = None
            if isinstance(ts, str) and ts:
                parsed_ts = parse_datetime(ts)
            elif isinstance(ts, datetime):
                parsed_ts = ts
            EquipmentMeasurement.objects.create(
                result=result,
                name=str(row.get("name") or row.get("measurement_name") or "measurement")[:255],
                value=str(row.get("value") or "")[:255],
                unit=str(row.get("unit") or "")[:64],
                pass_fail=str(row.get("pass_fail") or row.get("passFail") or "")[:32],
                tolerance=str(row.get("tolerance") or "")[:128],
                timestamp=parsed_ts,
                channel=str(row.get("channel") or "")[:128],
                remarks=str(row.get("remarks") or ""),
            )

        job.status = ResultProcessingStatus.LINKING_ATTACHMENTS
        job.save(update_fields=["status", "updated_at"])

        file_name = data.get("file_name") or (upload_session.file_name if upload_session else "result.bin")
        ext = normalize_extension(file_name)
        kind = "pdf" if ext == ".pdf" else "zip" if ext == ".zip" else "primary"
        if ext in ATTACHMENT_ONLY_EXTENSIONS:
            kind = ext.lstrip(".")

        attachment = ResultAttachment.objects.create(
            result=result,
            upload_session=upload_session,
            file_name=file_name,
            relative_path=data.get("relative_path") or "",
            content_type=data.get("content_type") or "",
            size_bytes=int(data.get("size_bytes") or (upload_session.expected_size if upload_session else 0) or 0),
            sha256=data.get("sha256") or "",
            storage_path=(upload_session.server_path if upload_session else "") or data.get("storage_path") or "",
            attachment_kind=kind,
        )

        # After DB commit: publish to S3 Results/{virtual_booking_id}/ and remove portal temp copy.
        attachment_id = attachment.id
        booking_pk = booking.pk
        transaction.on_commit(
            lambda aid=attachment_id, bid=booking_pk: _publish_attachment_to_s3_and_cleanup(aid, bid)
        )

        job.version = (job.version or 0) + 1
        job.save(update_fields=["version", "updated_at"])

        self._hooks.audit(
            sync_agent=agent,
            event_code=EVENT_RESULT_IMPORTED,
            message="Equipment result imported.",
            correlation_id=correlation_id,
            payload={
                "result_id": str(result.id),
                "measurements": len(measurements),
                "attachment_id": str(attachment.id),
                "parser": result.parser_used,
            },
        )

        return {
            "decision": "imported",
            "result_id": str(result.id),
            "duplicate": False,
            "measurement_count": len(measurements),
            "attachment_id": str(attachment.id),
            "processing_job_id": str(job.id),
        }

    def mark_failed(
        self,
        agent: DepartmentSyncAgent,
        *,
        agent_upload_id: uuid.UUID,
        error_message: str,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        job = ResultProcessingQueue.objects.filter(
            sync_agent=agent,
            agent_upload_id=agent_upload_id,
        ).first()
        if job is None:
            return
        job.status = ResultProcessingStatus.FAILED
        job.error_message = error_message[:4000]
        job.save(update_fields=["status", "error_message", "updated_at"])
        self._hooks.audit(
            sync_agent=agent,
            event_code=EVENT_RESULT_FAILED,
            message=error_message,
            correlation_id=correlation_id,
            payload={"agent_upload_id": str(agent_upload_id)},
        )
