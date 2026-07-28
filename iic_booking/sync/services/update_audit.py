"""Update audit trail helpers (Milestone 16)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from iic_booking.sync.models import DepartmentSyncAgent, SyncLogCategory, SyncLogSeverity
from iic_booking.sync.services.logging import write_sync_log

logger = logging.getLogger(__name__)


class UpdateAuditService:
    def write(
        self,
        *,
        event_code: str,
        message: str,
        sync_agent: DepartmentSyncAgent | None = None,
        correlation_id=None,
        department_id=None,
        building_id=None,
        version: str = "",
        details: dict[str, Any] | None = None,
        severity: str = SyncLogSeverity.INFO,
    ) -> None:
        payload = {
            "department_id": str(department_id) if department_id else None,
            "building_id": str(building_id) if building_id else None,
            "version": version,
            **(details or {}),
        }
        if sync_agent is not None:
            write_sync_log(
                event_code=event_code,
                category=SyncLogCategory.UPDATES,
                severity=severity,
                message=message,
                sync_agent=sync_agent,
                correlation_id=correlation_id,
                json_payload=payload,
            )
        logger.info(
            "update_audit event=%s message=%s agent=%s version=%s correlation=%s",
            event_code,
            message,
            getattr(sync_agent, "id", None),
            version,
            correlation_id,
        )
