"""SyncLog helpers for control-plane APIs."""

from __future__ import annotations

import logging
import threading
import uuid
from typing import Any

from iic_booking.sync.models import (
    DepartmentSyncAgent,
    SyncLog,
    SyncLogSeverity,
)

logger = logging.getLogger(__name__)


def write_sync_log(
    *,
    event_code: str,
    message: str,
    category: str,
    severity: str = SyncLogSeverity.INFO,
    sync_agent: DepartmentSyncAgent | None = None,
    equipment=None,
    correlation_id: uuid.UUID | None = None,
    json_payload: dict[str, Any] | None = None,
    durable: bool = False,
) -> SyncLog | None:
    """
    Persist a SyncLog row.

    When sync_agent is missing (e.g. unknown agent), skip persistence because
    SyncLog.sync_agent is required.

    Set durable=True for auth-failure paths so the row survives ATOMIC_REQUESTS
    rollback when AuthenticationFailed is raised.
    """
    if sync_agent is None:
        return None

    agent_id = sync_agent.pk
    equipment_id = getattr(equipment, "pk", None)
    payload = json_payload or {}

    def _create() -> SyncLog:
        return SyncLog.objects.create(
            sync_agent_id=agent_id,
            equipment_id=equipment_id,
            event_code=event_code,
            severity=severity,
            category=category,
            message=message,
            json_payload=payload,
            correlation_id=correlation_id,
        )

    if not durable:
        return _create()

    result: list[SyncLog] = []

    def _run():
        try:
            result.append(_create())
        except Exception:
            logger.exception("Durable SyncLog write failed for %s", event_code)

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    thread.join(timeout=10)
    return result[0] if result else None


# Stable event codes (Milestone 4 control plane)
EVENT_AGENT_ENROLLED = "SYNC-1001"
EVENT_ENROLLMENT_FAILED = "SYNC-1002"
EVENT_HEARTBEAT_RECEIVED = "SYNC-1101"
EVENT_HEARTBEAT_TIMEOUT = "SYNC-1102"
EVENT_BOOTSTRAP_GENERATED = "BOOTSTRAP-2001"
EVENT_AUTH_FAILED = "AUTH-3001"
EVENT_BOOTSTRAP_REQUIRED = "BOOTSTRAP-2002"

# Milestone 5 — operational data plane
EVENT_EQUIPMENT_SYNCED = "SYNC-4001"
EVENT_BOOKINGS_DOWNLOADED = "SYNC-4002"
EVENT_WORKSPACE_CREATED = "SYNC-4003"
EVENT_WORKSPACE_EXISTS = "SYNC-4004"
EVENT_COMMANDS_DOWNLOADED = "SYNC-4005"
EVENT_COMMAND_ACKNOWLEDGED = "SYNC-4006"
EVENT_COMMAND_COMPLETED = "SYNC-4007"
EVENT_COMMAND_FAILED = "SYNC-4008"

# Milestone 9 — upload transport
EVENT_UPLOAD_STARTED = "UPLOAD-1001"
EVENT_UPLOAD_CHUNK = "UPLOAD-1002"
EVENT_UPLOAD_COMPLETED = "UPLOAD-1003"
EVENT_UPLOAD_FAILED = "UPLOAD-1004"
EVENT_UPLOAD_RETRIED = "UPLOAD-1005"
EVENT_UPLOAD_CANCELLED = "UPLOAD-1006"

# Milestone 10 — result processing
EVENT_RESULT_IMPORTED = "RESULT-1001"
EVENT_RESULT_FINALIZED = "RESULT-1002"
EVENT_RESULT_FAILED = "RESULT-1003"

# Milestone 12 — security
EVENT_DEVICE_REGISTERED = "AUTH-3010"
EVENT_DEVICE_REGISTRATION_FAILED = "AUTH-3011"
EVENT_CERTIFICATE_ISSUED = "AUTH-3012"
EVENT_CERTIFICATE_RENEWED = "AUTH-3013"
EVENT_CERTIFICATE_REVOKED = "AUTH-3014"
EVENT_SIGNATURE_INVALID = "AUTH-3016"
EVENT_API_KEY_ROTATED = "AUTH-3019"

# Milestone 15 — monitoring / alerts
EVENT_MONITORING_INGEST = "MON-INGEST"
EVENT_MONITORING_ALERT = "MON-ALERT"
EVENT_MONITORING_ACK = "MON-ACK"
EVENT_MONITORING_RESOLVE = "MON-RESOLVE"

# Milestone 16 — updates
EVENT_UPDATE_CREATE = "UPD-CREATE"
EVENT_UPDATE_PUBLISH = "UPD-PUBLISH"
EVENT_UPDATE_DEPLOY = "UPD-DEPLOY"
EVENT_UPDATE_ROLLBACK = "UPD-ROLLBACK"
EVENT_UPDATE_STATUS = "UPD-STATUS"
