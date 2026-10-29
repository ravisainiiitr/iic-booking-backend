"""Administrative warnings for courier tracking, deduplicated by key and shown on the Main Administrator dashboard."""

from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from .models import CourierTrackingAlert

logger = logging.getLogger(__name__)

ADMIN_LINK = "/admin/courier-tracking"


def main_admins():
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    return list(User.objects.filter(is_active=True, user_type=UserType.ADMIN).order_by("id"))


def raise_alert(kind: str, dedupe_key: str, message: str, *, arrival=None, notify: bool = True) -> CourierTrackingAlert | None:
    """Record a warning once per ``dedupe_key``; the first time, tell Main Administrators in the portal."""
    try:
        with transaction.atomic():
            alert, created = CourierTrackingAlert.objects.get_or_create(
                dedupe_key=dedupe_key[:128],
                defaults={"kind": kind, "message": message[:500], "arrival": arrival},
            )
    except IntegrityError:
        return CourierTrackingAlert.objects.filter(dedupe_key=dedupe_key[:128]).first()
    if created:
        logger.warning("Courier tracking alert %s: %s", kind, message)
        if notify:
            try:
                from iic_booking.communication.in_app import notify_in_app

                notify_in_app(
                    main_admins(),
                    title="Courier tracking needs attention",
                    message=message,
                    link=ADMIN_LINK,
                    notification_type="warning",
                    event="courier_tracking.alert",
                )
            except Exception:  # noqa: BLE001 - an alert must never break the operation that raised it
                logger.exception("Could not notify administrators of courier tracking alert %s", dedupe_key)
    return alert
