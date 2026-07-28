"""Notification / audit hooks for result processing (delivery later)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from iic_booking.sync.models import DepartmentSyncAgent, SyncLogCategory, SyncLogSeverity
from iic_booking.sync.services.logging import write_sync_log

logger = logging.getLogger(__name__)

EVENT_RESULT_IMPORTED = "RESULT-1001"
EVENT_RESULT_FINALIZED = "RESULT-1002"
EVENT_RESULT_FAILED = "RESULT-1003"


class ResultNotificationHooks:
    """Placeholder interfaces for Email / SMS / WhatsApp / Push — audit only for now."""

    def audit(
        self,
        *,
        sync_agent: DepartmentSyncAgent,
        event_code: str,
        message: str,
        correlation_id: uuid.UUID | None = None,
        payload: dict[str, Any] | None = None,
        severity: str = SyncLogSeverity.INFO,
    ) -> None:
        write_sync_log(
            event_code=event_code,
            message=message,
            category=SyncLogCategory.OTHER,
            severity=severity,
            sync_agent=sync_agent,
            correlation_id=correlation_id,
            json_payload=payload or {},
        )
        logger.info("%s %s", event_code, message)

    def booking_activity(self, *, booking_id: int, message: str, payload: dict[str, Any] | None = None) -> None:
        logger.info("BookingActivity booking=%s %s payload=%s", booking_id, message, payload or {})

    def system_event(self, *, message: str, payload: dict[str, Any] | None = None) -> None:
        logger.info("SystemEvent %s payload=%s", message, payload or {})

    def enqueue_email(self, *, to: str, subject: str, body: str) -> None:
        logger.debug("Email placeholder to=%s subject=%s", to, subject)

    def enqueue_sms(self, *, to: str, body: str) -> None:
        logger.debug("SMS placeholder to=%s", to)

    def enqueue_whatsapp(self, *, to: str, body: str) -> None:
        logger.debug("WhatsApp placeholder to=%s", to)

    def enqueue_push(self, *, user_id: int, title: str, body: str) -> None:
        logger.debug("Push placeholder user=%s title=%s", user_id, title)
