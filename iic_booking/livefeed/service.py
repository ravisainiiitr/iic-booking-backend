"""Live feed sessions and signalling.

- The Lab Operator starts the feed at the instrument PC; the sync agent calls ``start`` for that instrument and gets
  a session only while a booking's slot is in progress (from 10 minutes before the slot to its end).
- The booking owner (only) sees the session and sends a WebRTC offer per source; the agent polls, answers each
  offer, and media then flows directly between the browser and the agent. Nothing is recorded.
- A session ends when the operator stops it, when the slot ends, or when the agent stops polling for a minute.
"""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from .models import LiveFeedPeer, LiveFeedSession

EARLY_START = timedelta(minutes=10)
AGENT_SILENT_AFTER = timedelta(seconds=60)
OFFER_TIMEOUT = timedelta(seconds=30)
MAX_SDP = 20_000
SOURCES = (LiveFeedPeer.SCREEN, LiveFeedPeer.CAMERA)


class LiveFeedError(Exception):
    pass


def current_booking(equipment, now=None):
    """(booking, slot_end) for the booking whose slot is in progress on this instrument, or (None, None)."""
    from django.db.models import Max, Min

    from iic_booking.equipment.models import BookingStatus, DailySlot

    now = now or timezone.now()
    rows = (DailySlot.objects.filter(slot_master__equipment=equipment, booking__isnull=False,
                                     booking__status=BookingStatus.BOOKED)
            .values("booking_id").annotate(start=Min("start_datetime"), end=Max("end_datetime"))
            .filter(start__lte=now + EARLY_START, end__gt=now).order_by("start"))
    row = rows.first()
    if row is None:
        return None, None
    from iic_booking.equipment.models import Booking

    return Booking.objects.select_related("user").get(pk=row["booking_id"]), row["end"]


def expire(now=None) -> int:
    """End sessions whose slot is over or whose agent went quiet; fail offers nobody answered."""
    now = now or timezone.now()
    ended = 0
    for s in LiveFeedSession.objects.filter(status=LiveFeedSession.ACTIVE):
        if s.slot_end <= now:
            end(s, "The booking slot ended.", now=now)
            ended += 1
        elif (s.last_agent_poll_at or s.started_at) < now - AGENT_SILENT_AFTER:
            end(s, "The lab's sync agent stopped responding.", now=now)
            ended += 1
    LiveFeedPeer.objects.filter(status=LiveFeedPeer.OFFERED, created_at__lt=now - OFFER_TIMEOUT).update(
        status=LiveFeedPeer.FAILED, error="The lab did not answer in time.", closed_at=now)
    return ended


def end(session: LiveFeedSession, reason: str, *, now=None) -> None:
    now = now or timezone.now()
    with transaction.atomic():
        session.status = LiveFeedSession.ENDED
        session.ended_at = now
        session.end_reason = reason[:200]
        session.save(update_fields=["status", "ended_at", "end_reason"])
        session.peers.filter(status__in=(LiveFeedPeer.OFFERED, LiveFeedPeer.ANSWERED)).update(
            status=LiveFeedPeer.CLOSED, closed_at=now)


def start(agent, equipment_id, *, sources, audio: bool, equipment_pc: str = "") -> LiveFeedSession:
    from iic_booking.equipment.models import Equipment
    from iic_booking.sync.services.scope import agent_may_access_equipment

    try:
        equipment_id = int(equipment_id)
    except (TypeError, ValueError):
        raise LiveFeedError("Unknown instrument.") from None
    if not agent_may_access_equipment(agent, equipment_id):
        raise LiveFeedError("This sync agent is not assigned to that instrument.")
    sources = [s for s in SOURCES if s in (sources or [])]
    if not sources:
        raise LiveFeedError("Choose the screen, the camera, or both.")
    equipment = Equipment.objects.get(pk=equipment_id)
    booking, slot_end = current_booking(equipment)
    if booking is None:
        raise LiveFeedError("No booking slot is in progress on this instrument, so there is nobody to show the feed to.")
    for old in LiveFeedSession.objects.filter(equipment=equipment, status=LiveFeedSession.ACTIVE):
        end(old, "Replaced by a new live feed.")
    return LiveFeedSession.objects.create(
        equipment=equipment, booking=booking, sync_agent=agent, equipment_pc=str(equipment_pc or "")[:120],
        sources=sources, audio=bool(audio), slot_end=slot_end, last_agent_poll_at=timezone.now())


def agent_session(agent, session_id) -> LiveFeedSession:
    s = LiveFeedSession.objects.filter(pk=session_id, sync_agent=agent).select_related("booking__user").first()
    if s is None:
        raise LiveFeedError("Unknown live feed.")
    return s


def agent_poll(session: LiveFeedSession) -> dict:
    """What the agent has to do: answer new offers, drop closed viewers, stop when the session ended."""
    now = timezone.now()
    if session.status == LiveFeedSession.ACTIVE:
        session.last_agent_poll_at = now
        session.save(update_fields=["last_agent_poll_at"])
        expire(now)
        session.refresh_from_db()
    peers = session.peers.all()
    return {
        "status": session.status,
        "end_reason": session.end_reason,
        "viewer": _viewer_name(session),
        "slot_end": session.slot_end.isoformat(),
        "offers": [{"peer_id": p.pk, "kind": p.kind, "sdp": p.offer_sdp}
                   for p in peers if p.status == LiveFeedPeer.OFFERED],
        "open": [p.pk for p in peers if p.status == LiveFeedPeer.ANSWERED],
    }


def answer(session: LiveFeedSession, peer_id, sdp: str = "", error: str = "") -> None:
    peer = session.peers.filter(pk=peer_id, status=LiveFeedPeer.OFFERED).first()
    if peer is None:
        raise LiveFeedError("That viewer connection is no longer waiting for an answer.")
    if error or not sdp:
        peer.status, peer.error, peer.closed_at = LiveFeedPeer.FAILED, (error or "No answer.")[:200], timezone.now()
    else:
        if len(sdp) > MAX_SDP:
            raise LiveFeedError("Answer too large.")
        peer.status, peer.answer_sdp, peer.answered_at = LiveFeedPeer.ANSWERED, sdp, timezone.now()
    peer.save()


def _viewer_name(session: LiveFeedSession) -> str:
    user = session.booking.user
    return (user.get_full_name() or "").strip() or "The booking owner"


# ---- viewer side --------------------------------------------------------------------------------------------------

def sessions_for(user):
    expire()
    return (LiveFeedSession.objects.filter(status=LiveFeedSession.ACTIVE, booking__user=user)
            .select_related("equipment", "booking"))


def viewer_session(user, session_id) -> LiveFeedSession:
    s = sessions_for(user).filter(pk=session_id).first()
    if s is None:
        raise LiveFeedError("This live feed is not available to you, or it has ended.")
    return s


def offer(user, session: LiveFeedSession, kind: str, sdp: str) -> LiveFeedPeer:
    if kind not in session.sources:
        raise LiveFeedError("That source is not being shared.")
    if not sdp or len(sdp) > MAX_SDP or not sdp.startswith("v=0"):
        raise LiveFeedError("Invalid offer.")
    now = timezone.now()
    # One connection per source and viewer: a new offer (reconnect, second tab) replaces the old one.
    session.peers.filter(viewer=user, kind=kind, status__in=(LiveFeedPeer.OFFERED, LiveFeedPeer.ANSWERED)).update(
        status=LiveFeedPeer.CLOSED, closed_at=now)
    return LiveFeedPeer.objects.create(session=session, viewer=user, kind=kind, offer_sdp=sdp)


def viewer_peer(user, peer_id) -> LiveFeedPeer:
    expire()
    peer = LiveFeedPeer.objects.filter(pk=peer_id, viewer=user).select_related("session").first()
    if peer is None:
        raise LiveFeedError("Unknown connection.")
    return peer


def close(peer: LiveFeedPeer) -> None:
    if peer.status in (LiveFeedPeer.OFFERED, LiveFeedPeer.ANSWERED):
        peer.status, peer.closed_at = LiveFeedPeer.CLOSED, timezone.now()
        peer.save(update_fields=["status", "closed_at"])
