"""Notification engine — Portal + Email delivery with user preferences."""

from __future__ import annotations

import logging
from datetime import datetime, time

from django.core.mail import send_mail
from django.conf import settings as django_settings
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AuditCategory,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from iic_booking.remote_analysis.collaboration_models import (
    CollaborationTelemetry,
    Notification,
    NotificationPreference,
)
from iic_booking.remote_analysis.services.audit import record_event

logger = logging.getLogger(__name__)


class NotificationEngine:
    def get_or_create_prefs(self, user) -> NotificationPreference:
        prefs, _ = NotificationPreference.objects.get_or_create(user=user)
        return prefs

    def _in_quiet_hours(self, prefs: NotificationPreference, now=None) -> bool:
        now = now or timezone.localtime()
        start, end = prefs.quiet_hours_start, prefs.quiet_hours_end
        if not start or not end:
            return False
        t = now.time()
        if start <= end:
            return start <= t <= end
        return t >= start or t <= end

    def _channel_allowed(self, prefs: NotificationPreference, channel: str) -> bool:
        if channel == NotificationChannel.PORTAL:
            return prefs.portal_enabled
        if channel == NotificationChannel.EMAIL:
            return prefs.email_enabled
        # Future channels default off
        return False

    def notify(
        self,
        user,
        notification_type: str,
        title: str,
        body: str = "",
        *,
        link: str = "",
        metadata: dict | None = None,
        channels: list[str] | None = None,
    ) -> list[Notification]:
        if not user or not getattr(user, "pk", None):
            return []
        prefs = self.get_or_create_prefs(user)
        if notification_type in (prefs.disabled_types or []):
            return []
        if self._in_quiet_hours(prefs) and notification_type not in {
            NotificationType.ALERT,
            NotificationType.ASSISTANCE,
            NotificationType.SESSION_TERMINATED,
        }:
            # Still deliver critical portal notifications
            channels = [NotificationChannel.PORTAL]

        channels = channels or [NotificationChannel.PORTAL, NotificationChannel.EMAIL]
        created: list[Notification] = []
        for channel in channels:
            if not self._channel_allowed(prefs, channel):
                continue
            row = Notification.objects.create(
                user=user,
                notification_type=notification_type,
                channel=channel,
                status=NotificationStatus.PENDING,
                title=title[:255],
                body=body,
                link=link[:1024],
                metadata=metadata or {},
            )
            ok = self._deliver(row)
            row.status = NotificationStatus.DELIVERED if ok else NotificationStatus.FAILED
            row.delivered_at = timezone.now() if ok else None
            row.save(update_fields=["status", "delivered_at"])
            CollaborationTelemetry.objects.create(
                metric_name="notification_delivery",
                value=1.0 if ok else 0.0,
                unit="bool",
                tags={"channel": channel, "type": notification_type},
            )
            created.append(row)
        if created:
            record_event(
                category=AuditCategory.NOTIFICATIONS,
                action="NotificationSent",
                details=title,
                actor=None,
                success=True,
            )
        return created

    def _deliver(self, row: Notification) -> bool:
        if row.channel == NotificationChannel.PORTAL:
            return True
        if row.channel == NotificationChannel.EMAIL:
            email = getattr(row.user, "email", "") or ""
            if not email:
                return False
            try:
                send_mail(
                    subject=row.title,
                    message=row.body or row.title,
                    from_email=getattr(django_settings, "DEFAULT_FROM_EMAIL", "noreply@localhost"),
                    recipient_list=[email],
                    fail_silently=True,
                )
                return True
            except Exception:
                logger.exception("Email notification failed")
                return False
        # SMS / WhatsApp / Push — future stubs
        return False

    def list_for_user(self, user, *, unread_only: bool = False, limit: int = 100) -> list[Notification]:
        qs = Notification.objects.filter(user=user, channel=NotificationChannel.PORTAL)
        if unread_only:
            qs = qs.filter(status__in=[NotificationStatus.PENDING, NotificationStatus.DELIVERED])
        return list(qs.order_by("-created_at")[:limit])

    def mark_read(self, user, notification_ids: list | None = None, *, all_unread: bool = False) -> int:
        qs = Notification.objects.filter(user=user, channel=NotificationChannel.PORTAL).exclude(
            status=NotificationStatus.READ
        )
        if notification_ids:
            qs = qs.filter(id__in=notification_ids)
        elif not all_unread:
            return 0
        now = timezone.now()
        count = qs.update(status=NotificationStatus.READ, read_at=now)
        CollaborationTelemetry.objects.create(
            metric_name="notification_read",
            value=float(count),
            unit="count",
        )
        return count
