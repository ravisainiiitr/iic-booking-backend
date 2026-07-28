"""Audit bridge for booking ↔ remote analysis integration events."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class BookingAuditBridge:
    def log(self, booking, action: str, *, details: str = "", actor=None, success: bool = True) -> None:
        try:
            from iic_booking.remote_analysis.constants import AuditCategory
            from iic_booking.remote_analysis.services.audit import record_event

            record_event(
                category=AuditCategory.COLLABORATION,
                action=f"BookingRA:{action}",
                details=details or f"booking={getattr(booking, 'booking_id', booking)}",
                actor=actor,
                success=success,
                correlation_id=str(getattr(booking, "booking_id", "")),
            )
        except Exception:
            logger.exception("BookingAuditBridge failed action=%s", action)

        try:
            from iic_booking.equipment.booking_events import create_booking_event
            from iic_booking.equipment.models import BookingEventType

            # Reuse COMMENT-like event if COMPLETED-style enum lacks RA types — use metadata on STATUS_CHANGED path
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.COMMENT,
                comment=f"[Remote Analysis] {action}: {details}",
                metadata={"remote_analysis": True, "action": action},
                created_by=actor,
                send_notification=False,
            )
        except Exception:
            logger.debug("booking event write skipped for %s", action, exc_info=True)
