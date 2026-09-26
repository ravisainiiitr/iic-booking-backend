"""Best-effort in-app (notification bell) records for workflow events.

Failures are logged and never propagate: a notification must not break the action that triggered it.
Delivery is deferred until the surrounding transaction commits.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.db import transaction

logger = logging.getLogger(__name__)


def _unique_active(recipients: Iterable[Any]) -> list:
    seen: set[int] = set()
    out = []
    for user in recipients or []:
        uid = getattr(user, "id", None)
        if not uid or uid in seen or getattr(user, "is_active", True) is False:
            continue
        seen.add(uid)
        out.append(user)
    return out


def notify_in_app(
    recipients: Iterable[Any],
    *,
    title: str,
    message: str,
    link: str | None = None,
    notification_type: str = "info",
    event: str = "",
    created_by=None,
    extra: dict[str, Any] | None = None,
) -> None:
    users = _unique_active(recipients)
    if not users:
        return
    metadata: dict[str, Any] = {"notification_type": notification_type, **(extra or {})}
    if link:
        metadata["link"] = link
    if event:
        metadata["event"] = event
    title = (title or "").strip()[:250] or "Notification"
    message = (message or "").strip() or title

    def _send() -> None:
        from .service import CommunicationService

        for user in users:
            try:
                CommunicationService.send_push_notification(
                    recipient=user,
                    title=title,
                    message=message,
                    metadata=metadata,
                    created_by=created_by,
                )
            except Exception:
                logger.exception("in-app notification failed event=%s user_id=%s", event, getattr(user, "id", None))

    try:
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(_send)
        else:
            _send()
    except Exception:
        logger.exception("in-app notification dispatch failed event=%s", event)


def equipment_oic_users(equipment) -> list:
    """Officer in charge (managers) and active temporary OICs for the equipment."""
    if equipment is None:
        return []
    from django.utils import timezone

    from iic_booking.equipment.models import EquipmentManager, EquipmentTemporaryOIC

    eid = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    users = [em.manager for em in EquipmentManager.objects.filter(equipment_id=eid).select_related("manager")]
    users += [
        row.temporary_oic
        for row in EquipmentTemporaryOIC.objects.filter(equipment_id=eid, resume_at__gt=timezone.now()).select_related(
            "temporary_oic"
        )
    ]
    return _unique_active(users)


def person_label(user, fallback: str = "a user") -> str:
    if user is None:
        return fallback
    return (getattr(user, "name", "") or "").strip() or (getattr(user, "email", "") or "").strip() or fallback
