"""Mark booking remote analysis permanently closed after a live desktop session ends."""

from __future__ import annotations

import logging

from django.utils import timezone

from iic_booking.remote_analysis.constants import SessionStatus

logger = logging.getLogger(__name__)

# Session reached a usable desktop (Guacamole launched / connected). Prepare-only failures stay retriable.
LIVE_DESKTOP_STATUSES = frozenset(
    {
        SessionStatus.LAUNCHED,
        SessionStatus.CONNECTING,
        SessionStatus.CONNECTED,
        SessionStatus.ACTIVE,
        SessionStatus.IDLE,
        SessionStatus.DISCONNECTING,
    }
)

CLOSE_ON_FINAL = frozenset(
    {
        SessionStatus.TERMINATED,
        SessionStatus.EXPIRED,
        SessionStatus.COMPLETED,
    }
)


def session_reached_live_desktop(session) -> bool:
    """True if this session ever reached a live / launched desktop."""
    if getattr(session, "connected_at", None) or getattr(session, "launch_time", None):
        return True
    status = getattr(session, "status", None) or ""
    if status in LIVE_DESKTOP_STATUSES:
        return True
    try:
        from iic_booking.remote_analysis.session_models import SessionStateHistory

        return SessionStateHistory.objects.filter(
            session_id=session.pk,
            to_status__in=LIVE_DESKTOP_STATUSES,
        ).exists()
    except Exception:  # noqa: BLE001
        logger.exception("session_reached_live_desktop history check failed for %s", getattr(session, "pk", None))
        return False


def maybe_close_booking_analysis_after_session(session, *, final_status: str) -> bool:
    """
    Permanently close remote analysis for the booking when a live session ends.

    Returns True if analysis_closed_at was set (or already set).
    Does not close on FAILED (prepare/credentials) so the user can retry.
    """
    if final_status not in CLOSE_ON_FINAL:
        return False
    if not session_reached_live_desktop(session):
        return False

    booking = getattr(session, "booking", None)
    if booking is None and getattr(session, "booking_id", None):
        try:
            from iic_booking.equipment.models import Booking

            booking = Booking.objects.filter(pk=session.booking_id).first()
        except Exception:  # noqa: BLE001
            booking = None
    if booking is None:
        return False
    if getattr(booking, "analysis_closed_at", None):
        return True

    now = timezone.now()
    booking.analysis_closed_at = now
    try:
        booking.save(update_fields=["analysis_closed_at", "updated_at"])
    except Exception:  # noqa: BLE001
        logger.exception("Failed to set analysis_closed_at for booking %s", booking.pk)
        return False
    logger.info(
        "analysis_closed_at set booking=%s session=%s final=%s",
        booking.pk,
        session.pk,
        final_status,
    )
    return True


def ensure_analysis_closed_from_history(booking) -> bool:
    """
    Lazily set analysis_closed_at when a prior live desktop session already ended.

    Covers bookings that finished before this field existed.
    """
    if booking is None:
        return False
    if getattr(booking, "analysis_closed_at", None):
        return True
    try:
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession

        qs = (
            RemoteDesktopSession.objects.filter(booking_id=booking.pk)
            .filter(status__in=list(CLOSE_ON_FINAL))
            .order_by("-created_at")[:8]
        )
        for session in qs:
            if session_reached_live_desktop(session):
                return maybe_close_booking_analysis_after_session(
                    session, final_status=session.status
                )
    except Exception:  # noqa: BLE001
        logger.exception("ensure_analysis_closed_from_history failed for booking %s", getattr(booking, "pk", None))
    return False
