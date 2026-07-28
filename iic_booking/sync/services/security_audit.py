"""Security audit persistence helpers (Milestone 12)."""

from __future__ import annotations

import uuid
from typing import Any

from iic_booking.sync.models import DepartmentSyncAgent, SecurityAuditEvent, SyncLogCategory, SyncLogSeverity
from iic_booking.sync.services.logging import write_sync_log

EVENT_DEVICE_REGISTERED = "AUTH-3010"
EVENT_DEVICE_REGISTRATION_FAILED = "AUTH-3011"
EVENT_CERTIFICATE_ISSUED = "AUTH-3012"
EVENT_CERTIFICATE_RENEWED = "AUTH-3013"
EVENT_CERTIFICATE_REVOKED = "AUTH-3014"
EVENT_CERTIFICATE_EXPIRED = "AUTH-3015"
EVENT_SIGNATURE_INVALID = "AUTH-3016"
EVENT_SIGNATURE_MISSING = "AUTH-3017"
EVENT_REPLAY_DETECTED = "AUTH-3018"
EVENT_API_KEY_ROTATED = "AUTH-3019"
EVENT_PERMISSION_DENIED = "AUTH-3020"
EVENT_AUTHENTICATION_SUCCESS = "AUTH-3021"
EVENT_SECURITY_EXCEPTION = "AUTH-3022"


class SecurityAuditService:
    def write(
        self,
        *,
        event_code: str,
        message: str,
        sync_agent: DepartmentSyncAgent | None = None,
        device_id: uuid.UUID | None = None,
        agent_uuid: uuid.UUID | None = None,
        correlation_id: uuid.UUID | None = None,
        user_name: str = "",
        ip_address: str | None = None,
        details: dict[str, Any] | None = None,
        also_sync_log: bool = True,
        durable: bool = False,
    ) -> SecurityAuditEvent:
        event = SecurityAuditEvent.objects.create(
            sync_agent=sync_agent,
            event_code=event_code,
            message=message[:500],
            device_id=device_id or (sync_agent.device_id if sync_agent else None),
            agent_uuid=agent_uuid or (sync_agent.agent_uuid if sync_agent else None),
            correlation_id=correlation_id,
            user_name=(user_name or "")[:200],
            ip_address=ip_address or None,
            details=details or {},
        )
        if also_sync_log and sync_agent is not None:
            write_sync_log(
                event_code=event_code,
                message=message,
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING
                if event_code
                in {
                    EVENT_SIGNATURE_INVALID,
                    EVENT_SIGNATURE_MISSING,
                    EVENT_REPLAY_DETECTED,
                    EVENT_PERMISSION_DENIED,
                    EVENT_DEVICE_REGISTRATION_FAILED,
                    EVENT_SECURITY_EXCEPTION,
                }
                else SyncLogSeverity.INFO,
                sync_agent=sync_agent,
                correlation_id=correlation_id,
                json_payload=details or {},
                durable=durable,
            )
        return event
