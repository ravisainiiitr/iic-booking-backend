"""Resumable upload transport for Department Sync Agents (Milestone 9)."""

from __future__ import annotations

import logging
import secrets
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from iic_booking.sync.constants import (
    upload_chunk_size_bytes,
    upload_session_ttl_hours,
)
from iic_booking.sync.exceptions import SyncControlPlaneError
from iic_booking.sync.models import (
    AgentUploadChunk,
    AgentUploadSession,
    AgentUploadSessionStatus,
    BookingWorkspace,
    DepartmentSyncAgent,
    SyncLogCategory,
    SyncLogSeverity,
)
from iic_booking.sync.services.logging import (
    EVENT_UPLOAD_CANCELLED,
    EVENT_UPLOAD_CHUNK,
    EVENT_UPLOAD_COMPLETED,
    EVENT_UPLOAD_FAILED,
    EVENT_UPLOAD_RETRIED,
    EVENT_UPLOAD_STARTED,
    write_sync_log,
)

logger = logging.getLogger(__name__)


class UploadTransportError(SyncControlPlaneError):
    code = "UPLOAD_ERROR"
    status_code = 400
    default_message = "Upload transport error."


class UploadSessionNotFoundError(UploadTransportError):
    code = "UPLOAD_SESSION_NOT_FOUND"
    status_code = 404
    default_message = "Upload session not found."


class UploadSessionExpiredError(UploadTransportError):
    code = "UPLOAD_SESSION_EXPIRED"
    status_code = 410
    default_message = "Upload session expired."


class UploadTokenInvalidError(UploadTransportError):
    code = "UPLOAD_TOKEN_INVALID"
    status_code = 403
    default_message = "Invalid resume token."


class UploadRejectedError(UploadTransportError):
    code = "UPLOAD_REJECTED"
    status_code = 400
    default_message = "Upload rejected."


def _storage_root() -> Path:
    configured = getattr(settings, "DSA_UPLOAD_STORAGE_ROOT", None)
    if configured:
        root = Path(configured)
    else:
        root = Path(settings.MEDIA_ROOT) / "sync_uploads"
    root.mkdir(parents=True, exist_ok=True)
    return root


class UploadTransportService:
    """Portal upload session management — transport only, no booking completion."""

    def start(
        self,
        agent: DepartmentSyncAgent,
        *,
        agent_upload_id: uuid.UUID,
        file_name: str,
        relative_path: str = "",
        expected_size: int = 0,
        equipment_id: int | None = None,
        booking_id: int | None = None,
        workspace_id: uuid.UUID | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> dict:
        self.cleanup_expired(agent)

        existing = (
            AgentUploadSession.objects.filter(sync_agent=agent, agent_upload_id=agent_upload_id)
            .exclude(
                status__in=[
                    AgentUploadSessionStatus.COMPLETED,
                    AgentUploadSessionStatus.REJECTED,
                    AgentUploadSessionStatus.CANCELLED,
                    AgentUploadSessionStatus.EXPIRED,
                ]
            )
            .first()
        )
        if existing is not None:
            if existing.expires_at <= timezone.now():
                existing.status = AgentUploadSessionStatus.EXPIRED
                existing.save(update_fields=["status", "updated_at"])
            else:
                write_sync_log(
                    event_code=EVENT_UPLOAD_RETRIED,
                    message="Resuming existing upload session.",
                    category=SyncLogCategory.UPLOAD,
                    sync_agent=agent,
                    correlation_id=correlation_id,
                    json_payload={"session_id": str(existing.id), "bytes_received": existing.bytes_received},
                )
                return self._session_payload(existing)

        chunk_size = upload_chunk_size_bytes()
        expected_chunks = 0
        if expected_size > 0 and chunk_size > 0:
            expected_chunks = (expected_size + chunk_size - 1) // chunk_size

        resume_token = secrets.token_urlsafe(24)
        session_id = uuid.uuid4()
        agent_key = getattr(agent, "agent_uuid", None) or agent.id
        # Sanitize filename so Path never creates nested dirs from client input.
        safe_name = Path(file_name or "upload.bin").name or "upload.bin"
        relative_server = Path(str(agent_key)) / str(session_id) / safe_name
        try:
            absolute = _storage_root() / relative_server
            absolute.parent.mkdir(parents=True, exist_ok=True)
            absolute.touch(exist_ok=True)
        except OSError as exc:
            logger.exception("Upload storage path not writable: %s", absolute)
            raise UploadTransportError(
                f"Upload storage is not writable: {exc}",
                code="UPLOAD_STORAGE_ERROR",
            ) from exc

        workspace = None
        if workspace_id:
            workspace = BookingWorkspace.objects.filter(id=workspace_id, sync_agent=agent).first()

        # Invalid FKs previously caused uncaught IntegrityError → HTTP 500 and blocked
        # all booking-linked DSA uploads (IT uploads without booking_id still worked).
        resolved_equipment_id = self._resolve_equipment_fk(equipment_id)
        resolved_booking_id = self._resolve_booking_fk(booking_id)

        expires_at = timezone.now() + timedelta(hours=upload_session_ttl_hours())
        try:
            session = AgentUploadSession.objects.create(
                id=session_id,
                sync_agent=agent,
                agent_upload_id=agent_upload_id,
                equipment_id=resolved_equipment_id,
                booking_id=resolved_booking_id,
                workspace=workspace,
                file_name=safe_name,
                relative_path=relative_path or "",
                expected_size=max(0, int(expected_size or 0)),
                expected_chunk_count=expected_chunks,
                chunk_size=chunk_size,
                resume_token=resume_token,
                server_path=str(relative_server).replace("\\", "/"),
                status=AgentUploadSessionStatus.PENDING,
                expires_at=expires_at,
                correlation_id=correlation_id,
            )
        except IntegrityError:
            # Concurrent start or leftover row with same agent_upload_id — resume it.
            existing = (
                AgentUploadSession.objects.filter(sync_agent=agent, agent_upload_id=agent_upload_id)
                .order_by("-created_at")
                .first()
            )
            if existing is not None:
                logger.warning(
                    "Upload session create raced/unique for agent_upload_id=%s; resuming %s",
                    agent_upload_id,
                    existing.id,
                )
                return self._session_payload(existing)
            raise UploadTransportError(
                "Could not create upload session (constraint conflict).",
                code="UPLOAD_CREATE_CONFLICT",
            )

        write_sync_log(
            event_code=EVENT_UPLOAD_STARTED,
            message="Upload session started.",
            category=SyncLogCategory.UPLOAD,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={
                "session_id": str(session.id),
                "agent_upload_id": str(agent_upload_id),
                "file_name": session.file_name,
                "expected_size": session.expected_size,
                "chunk_size": session.chunk_size,
                "booking_id": resolved_booking_id,
                "equipment_id": resolved_equipment_id,
            },
        )
        return self._session_payload(session)

    @transaction.atomic
    def receive_chunk(
        self,
        agent: DepartmentSyncAgent,
        *,
        upload_id: uuid.UUID,
        resume_token: str,
        chunk_index: int,
        total_chunks: int,
        data: bytes,
        correlation_id: uuid.UUID | None = None,
    ) -> dict:
        session = self._get_active_session(agent, upload_id, resume_token)
        if chunk_index < 0:
            raise UploadTransportError("chunk_index must be >= 0.", code="INVALID_CHUNK_INDEX")
        if not data:
            raise UploadTransportError("Empty chunk payload.", code="EMPTY_CHUNK")

        if total_chunks > 0:
            session.expected_chunk_count = total_chunks

        existing = AgentUploadChunk.objects.filter(session=session, chunk_index=chunk_index).first()
        if existing is not None:
            # Idempotent resume: already have this chunk.
            return {
                "status": "accepted",
                "chunk_index": chunk_index,
                "bytes_received": session.bytes_received,
                "chunks_received": session.chunks_received,
                "duplicate": True,
            }

        absolute = _storage_root() / session.server_path
        absolute.parent.mkdir(parents=True, exist_ok=True)
        offset = chunk_index * session.chunk_size
        with absolute.open("r+b") as handle:
            handle.seek(offset)
            handle.write(data)

        AgentUploadChunk.objects.create(
            session=session,
            chunk_index=chunk_index,
            size=len(data),
            checksum="",  # future SHA-256
        )
        session.bytes_received = (session.bytes_received or 0) + len(data)
        session.chunks_received = (session.chunks_received or 0) + 1
        session.status = AgentUploadSessionStatus.RECEIVING
        session.version = (session.version or 0) + 1
        session.save(
            update_fields=[
                "bytes_received",
                "chunks_received",
                "expected_chunk_count",
                "status",
                "version",
                "updated_at",
            ]
        )

        write_sync_log(
            event_code=EVENT_UPLOAD_CHUNK,
            message="Chunk uploaded.",
            category=SyncLogCategory.UPLOAD,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={
                "session_id": str(session.id),
                "chunk_index": chunk_index,
                "size": len(data),
                "bytes_received": session.bytes_received,
            },
        )
        return {
            "status": "accepted",
            "chunk_index": chunk_index,
            "bytes_received": session.bytes_received,
            "chunks_received": session.chunks_received,
            "duplicate": False,
        }

    @transaction.atomic
    def complete(
        self,
        agent: DepartmentSyncAgent,
        *,
        upload_id: uuid.UUID,
        resume_token: str,
        expected_size: int | None = None,
        chunk_count: int | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> dict:
        session = self._get_active_session(agent, upload_id, resume_token)
        session.status = AgentUploadSessionStatus.VERIFYING
        session.save(update_fields=["status", "updated_at"])

        size = int(expected_size if expected_size is not None else session.expected_size)
        chunks = int(chunk_count if chunk_count is not None else session.expected_chunk_count)

        absolute = _storage_root() / session.server_path
        actual_size = absolute.stat().st_size if absolute.exists() else 0
        actual_chunks = session.chunks.count()

        if size > 0 and actual_size != size:
            return self._reject(
                session,
                agent,
                reason=f"Size mismatch: expected {size}, got {actual_size}.",
                decision="retry",
                correlation_id=correlation_id,
            )
        if chunks > 0 and actual_chunks != chunks:
            return self._reject(
                session,
                agent,
                reason=f"Chunk count mismatch: expected {chunks}, got {actual_chunks}.",
                decision="retry",
                correlation_id=correlation_id,
            )
        if session.bytes_received != actual_size and size == 0:
            # Soft check when expected size was unknown at start.
            pass

        session.status = AgentUploadSessionStatus.COMPLETED
        session.completed_at = timezone.now()
        session.expected_size = actual_size
        session.expected_chunk_count = actual_chunks
        session.version = (session.version or 0) + 1
        session.save(
            update_fields=[
                "status",
                "completed_at",
                "expected_size",
                "expected_chunk_count",
                "version",
                "updated_at",
            ]
        )

        write_sync_log(
            event_code=EVENT_UPLOAD_COMPLETED,
            message="Upload completed.",
            category=SyncLogCategory.UPLOAD,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={
                "session_id": str(session.id),
                "bytes": actual_size,
                "chunks": actual_chunks,
                "server_path": session.server_path,
            },
        )

        # Make booking Results available without requiring a separate DSA import round-trip.
        self._auto_import_booking_result(agent, session, correlation_id=correlation_id)

        return {
            "decision": "completed",
            "upload_id": str(session.id),
            "server_path": session.server_path,
            "bytes": actual_size,
            "chunks": actual_chunks,
        }

    def cleanup_expired(self, agent: DepartmentSyncAgent | None = None) -> int:
        qs = AgentUploadSession.objects.filter(
            expires_at__lt=timezone.now(),
            status__in=[
                AgentUploadSessionStatus.PENDING,
                AgentUploadSessionStatus.RECEIVING,
                AgentUploadSessionStatus.VERIFYING,
            ],
        )
        if agent is not None:
            qs = qs.filter(sync_agent=agent)
        count = 0
        for session in qs.iterator():
            session.status = AgentUploadSessionStatus.EXPIRED
            session.last_error = "Session expired."
            session.save(update_fields=["status", "last_error", "updated_at"])
            count += 1
        return count

    def _reject(
        self,
        session: AgentUploadSession,
        agent: DepartmentSyncAgent,
        *,
        reason: str,
        decision: str,
        correlation_id: uuid.UUID | None,
    ) -> dict:
        session.status = AgentUploadSessionStatus.REJECTED if decision == "rejected" else AgentUploadSessionStatus.FAILED
        session.last_error = reason
        session.save(update_fields=["status", "last_error", "updated_at"])
        write_sync_log(
            event_code=EVENT_UPLOAD_FAILED,
            message=reason,
            category=SyncLogCategory.UPLOAD,
            severity=SyncLogSeverity.WARNING,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={"session_id": str(session.id), "decision": decision},
        )
        return {
            "decision": decision,
            "upload_id": str(session.id),
            "reason": reason,
        }

    def _get_active_session(
        self,
        agent: DepartmentSyncAgent,
        upload_id: uuid.UUID,
        resume_token: str,
    ) -> AgentUploadSession:
        session = AgentUploadSession.objects.filter(sync_agent=agent, id=upload_id).first()
        if session is None:
            # Also allow lookup by agent_upload_id for resume convenience.
            session = AgentUploadSession.objects.filter(sync_agent=agent, agent_upload_id=upload_id).first()
        if session is None:
            raise UploadSessionNotFoundError()
        if session.resume_token != resume_token:
            raise UploadTokenInvalidError()
        if session.expires_at <= timezone.now():
            session.status = AgentUploadSessionStatus.EXPIRED
            session.save(update_fields=["status", "updated_at"])
            raise UploadSessionExpiredError()
        if session.status in {
            AgentUploadSessionStatus.COMPLETED,
            AgentUploadSessionStatus.CANCELLED,
            AgentUploadSessionStatus.EXPIRED,
            AgentUploadSessionStatus.REJECTED,
        }:
            raise UploadTransportError(
                f"Session is {session.status}.",
                code="UPLOAD_SESSION_CLOSED",
            )
        return session

    def _session_payload(self, session: AgentUploadSession) -> dict:
        return {
            "upload_id": str(session.id),
            "agent_upload_id": str(session.agent_upload_id),
            "chunk_size": session.chunk_size,
            "resume_token": session.resume_token,
            "server_path": session.server_path,
            "expiration": session.expires_at.isoformat().replace("+00:00", "Z"),
            "bytes_received": session.bytes_received,
            "chunks_received": session.chunks_received,
            "status": session.status,
        }

    @staticmethod
    def _resolve_equipment_fk(equipment_id: int | None) -> int | None:
        if equipment_id is None:
            return None
        from iic_booking.equipment.models import Equipment

        if Equipment.objects.filter(pk=equipment_id).exists():
            return equipment_id
        logger.warning("Upload start: equipment_id=%s not found; omitting FK", equipment_id)
        return None

    @staticmethod
    def _resolve_booking_fk(booking_id: int | None) -> int | None:
        if booking_id is None:
            return None
        from iic_booking.equipment.models import Booking

        if Booking.objects.filter(pk=booking_id).exists():
            return booking_id
        logger.warning("Upload start: booking_id=%s not found; omitting FK", booking_id)
        return None

    def _auto_import_booking_result(
        self,
        agent: DepartmentSyncAgent,
        session: AgentUploadSession,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> None:
        """Register completed upload as a downloadable booking result when booking is known."""
        booking_id = session.booking_id
        equipment_id = session.equipment_id
        if not booking_id:
            return
        if not equipment_id:
            # Prefer booking's equipment when start() had to drop a bad equipment FK.
            from iic_booking.equipment.models import Booking

            booking = Booking.objects.filter(pk=booking_id).only("equipment_id").first()
            equipment_id = booking.equipment_id if booking else None
        if not equipment_id:
            logger.warning(
                "Upload complete: session %s has booking_id=%s but no equipment; skip auto-import",
                session.id,
                booking_id,
            )
            return

        try:
            from iic_booking.sync.services.result_processing import ResultProcessingService

            ResultProcessingService().import_results(
                agent,
                {
                    "agent_upload_id": str(session.agent_upload_id),
                    "booking_id": int(booking_id),
                    "equipment_id": int(equipment_id),
                    "upload_session_id": str(session.id),
                    "file_name": session.file_name,
                    "relative_path": session.relative_path or "",
                    "size_bytes": int(session.expected_size or session.bytes_received or 0),
                    "storage_path": session.server_path or "",
                    "parser_used": "upload_complete_auto",
                    "metadata": {},
                    "measurements": [],
                },
                correlation_id=correlation_id,
            )
        except Exception:
            # Transport already succeeded; DSA may still call /results/import/ later.
            logger.exception(
                "Auto-import after upload complete failed | session=%s booking=%s",
                session.id,
                booking_id,
            )
