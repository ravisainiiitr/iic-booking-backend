"""Tell a Remote Analysis user, on every channel they use, that their queue state changed.

The Remote Analysis notification engine already records a portal row and sends a plain email. These helpers add
the portal's main bell (so the notice appears where people look) and WhatsApp (approved templates only), and
write wording that says what happened, until when, and what to do next. Nothing here ever raises.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)


def _booking(reservation):
    return getattr(reservation, "booking", None) if getattr(reservation, "booking_id", None) else None


def workspace_link(reservation) -> str:
    booking = _booking(reservation)
    if booking is None:
        return "/remote-analysis"
    return f"/analysis-workspace/{booking.pk}"


def absolute_link(path: str) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}{path}" if base else path


def _label(reservation) -> str:
    booking = _booking(reservation)
    if booking is None:
        return "your Remote Analysis request"
    ref = getattr(booking, "virtual_booking_id", None) or booking.pk
    equipment = getattr(getattr(booking, "equipment", None), "name", "") or "equipment"
    return f"booking {ref} ({equipment})"


def _bell(reservation, *, title: str, message: str, event: str, kind: str = "info") -> None:
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [reservation.user],
            title=title,
            message=message,
            link=workspace_link(reservation),
            notification_type=kind,
            event=event,
            extra={"reservation_id": str(reservation.id)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Remote Analysis bell notice failed (%s) for reservation %s", event, reservation.pk)


def ready_message(reservation, minutes: int) -> tuple[str, str]:
    """Title and body for the seat-ready notice, shared by email, portal row and bell."""
    deadline = reservation.checkin_expires_at
    until = timezone.localtime(deadline).strftime("%I:%M %p").lstrip("0") if deadline else None
    title = "Your Analysis PC is ready"
    body = (
        f"An Analysis PC with the software for {_label(reservation)} is now held for you"
        f"{f' until {until}' if until else ''} ({minutes} minutes). "
        f"Open {absolute_link(workspace_link(reservation))} and choose Join session. "
        "If you do not join in time, the PC goes to the next person and you move to the end of the queue."
    )
    return title, body


def seat_ready(reservation, minutes: int) -> None:
    title, body = ready_message(reservation, minutes)
    _bell(reservation, title=title, message=body, event="remote_analysis_ready", kind="success")
    try:
        from iic_booking.whatsapp import hooks

        hooks.remote_analysis_ready(reservation, minutes)
    except Exception:  # noqa: BLE001
        logger.exception("Remote Analysis WhatsApp ready notice failed for reservation %s", reservation.pk)


def queued(reservation, *, position: int | None = None, wait_minutes: int | None = None) -> None:
    parts = [f"Every compatible Analysis PC is in use, so {_label(reservation)} is in the Remote Analysis queue."]
    if position:
        parts.append(f"You are number {position} in line.")
    if wait_minutes:
        parts.append(f"Estimated wait: about {wait_minutes} minutes.")
    parts.append("We will tell you here, by email and on WhatsApp (if you opted in) as soon as a PC is held for you.")
    _bell(reservation, title="You are in the Remote Analysis queue", message=" ".join(parts), event="remote_analysis_queued")


def notify_queue_expired(reservation) -> None:
    message = (
        f"The access window for {_label(reservation)} ended before an Analysis PC became free, "
        "so the request has left the queue. Open the booking to request analysis again if your access allows it."
    )
    _engine(reservation, "Remote Analysis request expired", message)
    _bell(reservation, title="Remote Analysis request expired", message=message, event="remote_analysis_expired", kind="warning")


def released(reservation) -> None:
    message = f"You released the Analysis PC held for {_label(reservation)}. It has gone to the next person in the queue."
    _bell(reservation, title="Analysis PC released", message=message, event="remote_analysis_released")


def _engine(reservation, title: str, body: str) -> None:
    try:
        from iic_booking.remote_analysis.constants import NotificationType
        from iic_booking.remote_analysis.notifications import NotificationEngine

        NotificationEngine().notify(
            reservation.user,
            NotificationType.RESERVATION_CONFIRMED,
            title,
            body,
            link=workspace_link(reservation),
            metadata={"reservation_id": str(reservation.id)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Remote Analysis notice failed for reservation %s", reservation.pk)
