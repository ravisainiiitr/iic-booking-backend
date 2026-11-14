"""Part 5: keep running while the PC is free.

The user closes the browser but leaves the Windows session logged on (disconnected) so long jobs keep running. Allowed
only when nobody is waiting for the PC and no reservation starts on it within 20 minutes. ``keep_running_until`` is
``next start - 10 min`` (or the booking's analysis access end). At ``keep_running_until`` the normal end path runs
(results to the outbox). Someone newly queued for this PC ends keep-running with the usual 2-minute finish-up
(``handover.FINISH_UP_MINUTES``): the PC is handed to them at once and the kept session ends two minutes later.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import CommandType, ReservationStatus, SessionStatus
from iic_booking.remote_analysis.large_data.common import CAP_KEEP_RUNNING, TransferError, agent_supports, iso

logger = logging.getLogger(__name__)

NEXT_START_BUFFER = timedelta(minutes=20)
END_BEFORE_NEXT = timedelta(minutes=10)
MAX_KEEP_HOURS = int(getattr(settings, "REMOTE_ANALYSIS_KEEP_RUNNING_MAX_HOURS", 24) or 24)
ALLOWED_FROM = {SessionStatus.LAUNCHED, SessionStatus.CONNECTING, SessionStatus.CONNECTED, SessionStatus.ACTIVE, SessionStatus.IDLE}
HOLDING = [
    ReservationStatus.RESERVED,
    ReservationStatus.AWAITING_CHECKIN,
    ReservationStatus.PREPARING,
    ReservationStatus.READY,
    ReservationStatus.ACTIVE,
]


def is_keeping(session, now=None) -> bool:
    now = now or timezone.now()
    return bool(session is not None and session.keep_running_until and session.keep_running_until > now)


def _next_start(session, now):
    """Earliest start of another reservation already placed on this PC, or None."""
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    starts = []
    for res in AnalysisReservation.objects.filter(workstation_id=session.workstation_id, status__in=HOLDING).exclude(
        pk=session.reservation_id
    ):
        starts.append(res.reserved_start or res.requested_start or now)
    return min(starts) if starts else None


def _blocked_reason(session, now) -> str | None:
    from iic_booking.remote_analysis.services import handover, queue_insight

    if session.status not in ALLOWED_FROM:
        return "Your session is not running."
    if not agent_supports(session.workstation, CAP_KEEP_RUNNING):
        return "This Analysis PC needs an agent update before a session can keep running after you leave."
    if handover.handover_state(session):
        return "The next user has been told this Analysis PC is ready, so the session cannot keep running."
    if queue_insight.someone_waiting_for(session.workstation_id, exclude_reservation_id=session.reservation_id):
        return "Someone is waiting for this Analysis PC, so the session cannot keep running."
    nxt = _next_start(session, now)
    if nxt is not None and nxt <= now + NEXT_START_BUFFER:
        return "Another booking starts on this Analysis PC within 20 minutes."
    return None


def status(session, now=None) -> dict[str, Any]:
    """``can_keep_running`` / reason for the session payload."""
    now = now or timezone.now()
    if session is None:
        return {"can_keep_running": False, "keep_running_blocked_reason": None, "keep_running_until": None}
    reason = None if is_keeping(session, now) else _blocked_reason(session, now)
    return {
        "can_keep_running": not is_keeping(session, now) and reason is None,
        "keep_running_blocked_reason": reason,
        "keep_running_until": iso(session.keep_running_until) if is_keeping(session, now) else None,
    }


@transaction.atomic
def start(session, *, user=None, now=None) -> dict[str, Any]:
    from iic_booking.remote_analysis.guacamole.connection import ConnectionManager
    from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationEvent
    from iic_booking.remote_analysis.services.commands import CommandService
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionToken

    now = now or timezone.now()
    session = RemoteDesktopSession.objects.select_for_update(of=("self",)).get(pk=session.pk)
    if is_keeping(session, now):
        return _response(session)
    reason = _blocked_reason(session, now)
    if reason:
        raise TransferError("keep_running_blocked", reason, 409)

    nxt = _next_start(session, now)
    booking = session.booking if session.booking_id else None
    access_end = getattr(booking, "analysis_expiry", None) if booking is not None else None
    candidates = [now + timedelta(hours=MAX_KEEP_HOURS)]
    if nxt is not None:
        candidates.append(nxt - END_BEFORE_NEXT)
    if access_end is not None:
        candidates.append(access_end)
    until = min(candidates)
    if until <= now + timedelta(minutes=1):
        raise TransferError("keep_running_blocked", "There is not enough time left to keep the session running.", 409)

    # End the browser connection; the Windows session stays logged on (disconnected).
    try:
        ConnectionManager().destroy(session)
    except Exception:  # noqa: BLE001
        logger.exception("Guacamole destroy failed for keep-running session %s", session.pk)
    SessionToken.objects.filter(session=session, consumed_at__isnull=True, revoked_at__isnull=True).update(revoked_at=now)
    session.keep_running_until = until
    session.expires_at = until
    session.save(update_fields=["keep_running_until", "expires_at", "updated_at"])
    # READY: reconnecting provisions a fresh Guacamole connection (see launch_session).
    SessionOrchestrator().transition(session, SessionStatus.READY, reason="Keep running: browser closed")

    if session.reservation_id:
        reservation = AnalysisReservation.objects.select_for_update().get(pk=session.reservation_id)
        end = reservation.reserved_end or reservation.requested_end
        if end is None or end < until:
            reservation.reserved_end = until
            reservation.save(update_fields=["reserved_end", "updated_at"])
        ReservationEvent.objects.create(
            reservation=reservation,
            event_type="KEEP_RUNNING",
            details=f"Keep running until {until.isoformat()}",
            metadata={"session_id": str(session.pk), "until": until.isoformat()},
        )
    CommandService().create_command(
        session.workstation,
        CommandType.KEEP_RUNNING,
        payload={"session_id": str(session.pk), "until": until.isoformat(), "logoff": False},
        created_by=user if getattr(user, "pk", None) else None,
        expires_in_hours=1,
    )
    return _response(session)


def _response(session) -> dict[str, Any]:
    return {
        "session_id": str(session.pk),
        "status": session.status,
        "keep_running_until": iso(session.keep_running_until),
        "expires_at": iso(session.expires_at),
        "detail": "Your session keeps running on the Analysis PC. Reconnect from the workspace page at any time.",
    }


def needs_reprovision(session) -> bool:
    """A kept session the user reconnects to: its Guacamole connection was closed."""
    if session is None or not session.keep_running_until or session.status != SessionStatus.READY:
        return False
    try:
        conn = session.guacamole_connection
    except Exception:  # noqa: BLE001 - no connection row
        return True
    return not conn.is_active or conn.destroyed_at is not None


def sweep(now=None) -> int:
    """End keep-running sessions someone now waits for: hand the PC over, 2-minute finish-up."""
    from iic_booking.remote_analysis.guacamole.cleanup import TERMINAL_STATUSES
    from iic_booking.remote_analysis.scheduler_models import ReservationEvent
    from iic_booking.remote_analysis.services import handover
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    now = now or timezone.now()
    finish_by = now + timedelta(minutes=handover.FINISH_UP_MINUTES)
    count = 0
    qs = RemoteDesktopSession.objects.filter(keep_running_until__gt=finish_by).exclude(status__in=list(TERMINAL_STATUSES))
    for session in qs.select_related("workstation", "reservation"):
        entry = handover._next_waiting_for(session.workstation_id, exclude_reservation_id=session.reservation_id)
        if entry is None:
            continue
        with transaction.atomic():
            if not handover._allocate_here(entry, session.workstation, now=now):
                continue
            RemoteDesktopSession.objects.filter(pk=session.pk).update(
                keep_running_until=finish_by, expires_at=finish_by, updated_at=now
            )
            if session.reservation_id:
                ReservationEvent.objects.create(
                    reservation_id=session.reservation_id,
                    event_type="KEEP_RUNNING_ENDED",
                    details=f"Someone needs this Analysis PC; finish by {finish_by.isoformat()}",
                    metadata={"next_reservation_id": str(entry.reservation_id), "finish_by": finish_by.isoformat()},
                )
        _tell_user(session, finish_by)
        count += 1
    return count


def _tell_user(session, finish_by) -> None:
    try:
        from iic_booking.communication.in_app import notify_in_app

        minutes = max(1, int((finish_by - timezone.now()).total_seconds() + 59) // 60)
        notify_in_app(
            [session.user],
            title="Your kept session ends soon",
            message=(
                f"Someone needs the Analysis PC your session was kept running on. It ends in about {minutes} "
                "minutes; your results then upload to My Research as usual. Reconnect now to save anything open."
            ),
            link=f"/analysis-workspace/{session.booking_id}" if session.booking_id else "/remote-analysis",
            notification_type="warning",
            event="remote_analysis_keep_running_ending",
            extra={"session_id": str(session.pk)},
        )
    except Exception:  # noqa: BLE001
        logger.exception("Keep-running ending notice failed for session %s", session.pk)
