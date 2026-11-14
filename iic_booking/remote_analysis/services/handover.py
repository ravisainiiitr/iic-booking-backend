"""No-show grace: the current user keeps working until the next user actually joins.

Worldwide practice for shared instruments: a booking only guarantees the PC is free when the next person
arrives. When a session's time is up and someone is waiting for this Analysis PC:

1. The PC is handed to the next person in the queue straight away (their check-in window opens and they are
   told the PC is ready).
2. The current session keeps running for up to ``NO_SHOW_GRACE_MINUTES`` (10) while that person has not
   joined, so nobody's work is cut off for a no-show.
3. When the next person joins, the current session gets ``FINISH_UP_MINUTES`` (2) to save, and the next
   person's desktop opens as soon as it ends.
4. If the next person does not join, their check-in lapses as usual and the current user may extend again
   (or the next person in the queue gets the same treatment).

State is recorded as ReservationEvent rows on the *current* reservation (no schema change):
``HANDOVER_STARTED`` (metadata: next_reservation_id, grace_until) and ``HANDOVER_JOINED``.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    QueueEntryStatus,
    ReservationStatus,
    SessionStatus,
    WorkstationStatus,
)

logger = logging.getLogger(__name__)

NO_SHOW_GRACE_MINUTES = int(getattr(settings, "REMOTE_ANALYSIS_NO_SHOW_GRACE_MINUTES", 10) or 10)
FINISH_UP_MINUTES = int(getattr(settings, "REMOTE_ANALYSIS_FINISH_UP_MINUTES", 2) or 2)

#: Sessions that are still on the PC (the user may be working).
LIVE_SESSION_STATUSES = {
    SessionStatus.CREATED,
    SessionStatus.PREPARING,
    SessionStatus.READY,
    SessionStatus.TOKEN_GENERATED,
    SessionStatus.LAUNCHED,
    SessionStatus.CONNECTING,
    SessionStatus.CONNECTED,
    SessionStatus.ACTIVE,
    SessionStatus.IDLE,
}

#: The next user holds the PC but has not started yet.
_NEXT_HOLD_STATUSES = {ReservationStatus.AWAITING_CHECKIN}


def _live_session_on(workstation_id, *, exclude_reservation_id=None):
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    qs = RemoteDesktopSession.objects.filter(workstation_id=workstation_id, status__in=LIVE_SESSION_STATUSES)
    if exclude_reservation_id is not None:
        qs = qs.exclude(reservation_id=exclude_reservation_id)
    return qs.order_by("-created_at").first()


def workstation_has_live_session(workstation_id, *, exclude_reservation_id=None) -> bool:
    return workstation_id is not None and _live_session_on(
        workstation_id, exclude_reservation_id=exclude_reservation_id
    ) is not None


def _latest_handover_event(reservation_id):
    from iic_booking.remote_analysis.scheduler_models import ReservationEvent

    return (
        ReservationEvent.objects.filter(
            reservation_id=reservation_id, event_type__in=["HANDOVER_STARTED", "HANDOVER_JOINED"]
        )
        .order_by("-created_at", "-id")
        .first()
    )


def handover_state(session) -> dict[str, Any] | None:
    """For the session page: is this session running on borrowed time while the next user is told?"""
    if session is None or not session.reservation_id or not session.workstation_id:
        return None
    event = _latest_handover_event(session.reservation_id)
    if event is None:
        return None
    meta = dict(event.metadata or {})
    next_id = meta.get("next_reservation_id")
    if not next_id:
        return None
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    nxt = AnalysisReservation.objects.filter(pk=next_id).only("status", "workstation_id").first()
    if nxt is None or nxt.workstation_id != session.workstation_id:
        return None  # the next user did not show and lost the PC
    if nxt.status in _NEXT_HOLD_STATUSES:
        return {"active": True, "next_joined": False, "ends_at": session.expires_at.isoformat() if session.expires_at else None}
    if nxt.status in {ReservationStatus.RESERVED, ReservationStatus.PREPARING, ReservationStatus.READY, ReservationStatus.ACTIVE}:
        return {"active": True, "next_joined": True, "ends_at": session.expires_at.isoformat() if session.expires_at else None}
    return None


def _next_waiting_for(workstation_id, *, exclude_reservation_id):
    """The first queued request that can use this PC (same order the scheduler uses)."""
    from iic_booking.remote_analysis.services.queue_insight import _Fleet, _waiting_entries, required_names

    fleet = _Fleet()
    for entry in _waiting_entries():
        if entry.reservation_id == exclude_reservation_id or entry.status != QueueEntryStatus.WAITING:
            continue
        if workstation_id in fleet.compatible(required_names(entry.reservation)):
            return entry
    return None


def _allocate_here(entry, workstation, *, now: datetime) -> bool:
    """Give the PC to ``entry`` while the previous session finishes; mirrors SchedulerService.allocate's tail."""
    from iic_booking.remote_analysis.scheduler_models import ReservationAudit, ReservationEvent
    from iic_booking.remote_analysis.services.checkin import CheckinService
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    scheduler = SchedulerService()
    reservation = entry.reservation
    if scheduler.cancel_if_booking_ineligible(reservation):
        return False
    if reservation.requested_end and reservation.requested_end <= now:
        return False
    reservation.workstation = workstation
    reservation.reserved_start = max(reservation.requested_start, now)
    reservation.reserved_end = reservation.requested_end
    reservation.allocated_at = now
    reservation.allocation_notes = f"Handed over {workstation.hostname} while the previous session finishes"
    reservation.save(
        update_fields=["workstation", "reserved_start", "reserved_end", "allocated_at", "allocation_notes", "updated_at"]
    )
    scheduler.reservations.transition(reservation, ReservationStatus.RESERVED, reason=reservation.allocation_notes)
    try:
        scheduler.queue.mark_reserved(entry)
    except Exception:  # noqa: BLE001
        logger.exception("Queue mark_reserved failed during handover for reservation %s", reservation.pk)
    CheckinService().open_checkin_window(reservation)
    ReservationAudit.objects.create(reservation=reservation, action="Allocated", details=reservation.allocation_notes)
    ReservationEvent.objects.create(reservation=reservation, event_type="ALLOCATED", details=reservation.allocation_notes)
    # The previous user is still on it.
    if workstation.status != WorkstationStatus.BUSY:
        workstation.status = WorkstationStatus.BUSY
        workstation.save(update_fields=["status", "updated_at"])
    return True


@transaction.atomic
def begin_if_waiting(session, *, now: datetime | None = None) -> bool:
    """Called when ``session`` reaches its end time. True when it was kept open for the no-show grace."""
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationEvent

    now = now or timezone.now()
    if session.status not in {SessionStatus.CONNECTED, SessionStatus.ACTIVE, SessionStatus.IDLE}:
        return False
    if not session.workstation_id or not session.reservation_id:
        return False
    # Someone already holds the PC after this session: their grace is running or they have joined.
    if AnalysisReservation.objects.filter(
        workstation_id=session.workstation_id,
        status__in=[
            ReservationStatus.AWAITING_CHECKIN,
            ReservationStatus.RESERVED,
            ReservationStatus.PREPARING,
            ReservationStatus.READY,
        ],
    ).exclude(pk=session.reservation_id).exists():
        return False
    entry = _next_waiting_for(session.workstation_id, exclude_reservation_id=session.reservation_id)
    if entry is None:
        return False

    grace_until = now + timedelta(minutes=NO_SHOW_GRACE_MINUTES)
    booking = getattr(session.reservation, "booking", None)
    access_end = getattr(booking, "analysis_expiry", None) if booking else None
    if access_end and access_end <= now:
        return False  # this booking's analysis access is over; nothing to borrow
    if access_end and grace_until > access_end:
        grace_until = access_end

    if not _allocate_here(entry, session.workstation, now=now):
        return False

    session.expires_at = grace_until
    session.save(update_fields=["expires_at", "updated_at"])
    current = session.reservation
    current_end = current.reserved_end or current.requested_end
    if current_end is None or current_end < grace_until:
        # Keep the current hold covering the session, else the natural-end sweep frees it mid-grace.
        current.reserved_end = grace_until
        current.save(update_fields=["reserved_end", "updated_at"])
    ReservationEvent.objects.create(
        reservation=current,
        event_type="HANDOVER_STARTED",
        details=f"Next user notified; grace until {grace_until.isoformat()}",
        metadata={"next_reservation_id": str(entry.reservation_id), "grace_until": grace_until.isoformat()},
    )
    logger.info(
        "Handover: session %s kept until %s while reservation %s checks in",
        session.pk,
        grace_until,
        entry.reservation_id,
    )
    return True


@transaction.atomic
def on_next_user_joining(reservation, *, now: datetime | None = None) -> dict[str, Any] | None:
    """The next user pressed Start. Give the previous session ``FINISH_UP_MINUTES`` and ask them to wait.

    Returns a launch-pending payload while the previous session is still on the PC, else None (go ahead).
    """
    from iic_booking.remote_analysis.scheduler_models import ReservationEvent
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    if not reservation.workstation_id:
        return None
    previous = _live_session_on(reservation.workstation_id, exclude_reservation_id=reservation.pk)
    if previous is None:
        return None
    now = now or timezone.now()
    previous = RemoteDesktopSession.objects.select_for_update().get(pk=previous.pk)
    finish_by = now + timedelta(minutes=FINISH_UP_MINUTES)
    if previous.expires_at is None or previous.expires_at > finish_by:
        previous.expires_at = finish_by
        previous.save(update_fields=["expires_at", "updated_at"])
        if previous.reservation_id:
            ReservationEvent.objects.create(
                reservation_id=previous.reservation_id,
                event_type="HANDOVER_JOINED",
                details=f"Next user joined; finish by {finish_by.isoformat()}",
                metadata={"next_reservation_id": str(reservation.pk), "finish_by": finish_by.isoformat()},
            )
    wait = max(0, int((previous.expires_at - now).total_seconds())) if previous.expires_at else 0
    minutes = max(1, (wait + 59) // 60)
    return {
        "launch_pending": True,
        "status": "WAITING_PREVIOUS_USER",
        "detail": (
            "The previous user is saving their work. Your Analysis PC opens in about "
            f"{minutes} minute{'s' if minutes != 1 else ''}."
        ),
        "retry_after_seconds": min(15, max(5, wait)),
    }
