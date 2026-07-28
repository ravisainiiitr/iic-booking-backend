"""Notification bridge — delegates to Remote Analysis NotificationEngine."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class BookingNotificationBridge:
    def notify(self, user, notification_type: str, title: str, body: str = "", *, metadata: dict | None = None) -> None:
        try:
            from iic_booking.remote_analysis.notifications import NotificationEngine

            NotificationEngine().notify(
                user,
                notification_type,
                title,
                body,
                metadata=metadata or {},
                channels=["PORTAL"],
            )
        except Exception:
            logger.exception("BookingNotificationBridge.notify failed")
