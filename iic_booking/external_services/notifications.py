"""Notifications for external-user services: a ledger row per (event, recipient, channel), sent after commit.

* ``notify`` is called inside the business transaction; rows are created with a unique key, so a repeated event
  (a retried task, a double click) never sends twice.
* Sending happens after commit, outside any transaction. A row is claimed (PENDING -> SENDING) by compare-and-swap,
  so two workers never send the same row. A row left SENDING by a crash is marked UNCERTAIN, never resent
  automatically (the message may have gone out).
* Email uses the existing ``CommunicationService`` with the ``external_service_notice`` template (test-account
  redirects and preferences apply); in-portal messages use ``send_push_notification``.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

TEMPLATE = "external_service_notice"
MAX_ATTEMPTS = 5
STALE_SENDING = timedelta(minutes=30)


def notify(key: str, recipients, *, title: str, message: str, link: str = "", email: bool = True) -> list[int]:
    """Queue a message to each recipient (in-portal always, email unless ``email=False``). Returns row ids."""
    from .models import ExternalNotification, NotificationChannel

    ids = []
    channels = [NotificationChannel.IN_APP] + ([NotificationChannel.EMAIL] if email else [])
    seen = set()
    for user in recipients:
        if user is None or user.pk in seen or not getattr(user, "is_active", True):
            continue
        seen.add(user.pk)
        for channel in channels:
            try:
                with transaction.atomic():
                    row, created = ExternalNotification.objects.get_or_create(
                        key=key[:160], recipient=user, channel=channel,
                        defaults={"title": title[:200], "message": message, "link": link[:300]},
                    )
            except IntegrityError:  # a concurrent identical event created it first
                continue
            if created:
                ids.append(row.pk)
    if ids:
        transaction.on_commit(lambda: send_rows(ids))
    return ids


def _claim(pk) -> bool:
    from .models import ExternalNotification, NotificationStatus

    return bool(
        ExternalNotification.objects.filter(
            pk=pk, status__in=[NotificationStatus.PENDING, NotificationStatus.FAILED], attempts__lt=MAX_ATTEMPTS
        ).update(status=NotificationStatus.SENDING, claimed_at=timezone.now())
    )


def _deliver(row) -> None:
    from iic_booking.communication.service import CommunicationService
    from iic_booking.users.display import get_user_display_name

    from .models import NotificationChannel

    if row.channel == NotificationChannel.EMAIL:
        log = CommunicationService.send_email(
            recipient=row.recipient,
            template=TEMPLATE,
            template_context={
                "user_name": get_user_display_name(row.recipient) or "User",
                "title": row.title,
                "message": row.message,
                "link": row.link,
            },
            metadata={"external_notification_id": row.pk},
        )
        if log is not None and getattr(log, "status", "") == "failed":
            raise RuntimeError("email send failed")
    else:
        CommunicationService.send_push_notification(
            recipient=row.recipient, title=row.title, message=row.message,
            metadata={"notification_type": "info", "event": "external_services", "link": row.link},
        )


def send_rows(ids) -> int:
    from .models import ExternalNotification, NotificationStatus

    sent = 0
    for pk in ids:
        if not _claim(pk):
            continue
        row = ExternalNotification.objects.select_related("recipient").get(pk=pk)
        try:
            _deliver(row)
        except Exception as exc:  # noqa: BLE001 - recorded on the row; retried by the sweep
            ExternalNotification.objects.filter(pk=pk).update(
                status=NotificationStatus.FAILED, attempts=row.attempts + 1, last_error=type(exc).__name__[:255]
            )
            logger.warning("External notification %s failed (%s)", pk, type(exc).__name__)
            continue
        ExternalNotification.objects.filter(pk=pk).update(
            status=NotificationStatus.SENT, attempts=row.attempts + 1, sent_at=timezone.now(), last_error=""
        )
        sent += 1
    return sent


def sweep() -> dict:
    """Retry failed rows; mark rows stuck in SENDING as UNCERTAIN (not resent)."""
    from .models import ExternalNotification, NotificationStatus

    stale = ExternalNotification.objects.filter(
        status=NotificationStatus.SENDING, claimed_at__lt=timezone.now() - STALE_SENDING
    ).update(status=NotificationStatus.UNCERTAIN)
    ids = list(
        ExternalNotification.objects.filter(
            status__in=[NotificationStatus.PENDING, NotificationStatus.FAILED], attempts__lt=MAX_ATTEMPTS
        ).values_list("pk", flat=True)[:200]
    )
    return {"uncertain": stale, "sent": send_rows(ids)}
