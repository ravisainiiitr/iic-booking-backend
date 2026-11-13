"""Where a Remote Analysis request stands in the queue, why it waits, and roughly how long.

The queue is one FIFO list (priority, then time joined), but requests only compete for the Analysis PCs that
have their software. Position, wait estimates and the "someone is waiting" fairness check therefore count only
requests that could use the same PCs. Without that, a request for different software inflated everyone's
position and blocked session extensions on PCs it could never use.

The wait estimate simulates the compatible PCs: each frees up when its current session ends (or now, if free),
and every request ahead of this one takes the next free PC for one average session.
"""

from __future__ import annotations

import heapq
import math
from datetime import datetime, timedelta
from typing import Any, Iterable

from django.utils import timezone

from iic_booking.remote_analysis.constants import QueueEntryStatus, ReservationStatus, SessionStatus, WorkstationStatus

OPEN_SESSION = {
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
UNUSABLE = {
    WorkstationStatus.MAINTENANCE,
    WorkstationStatus.DISABLED,
    WorkstationStatus.CALIBRATION,
    WorkstationStatus.SOFTWARE_UPDATE,
    WorkstationStatus.HARDWARE_FAULT,
}
MAINTENANCE_LIKE = UNUSABLE - {WorkstationStatus.DISABLED}

REASONS = {
    "ahead_in_queue": "Other people asked first for an Analysis PC with this software. You will get the next one that frees up.",
    "pcs_busy": "Every Analysis PC with this software is in use right now. The next one to free up is held for you.",
    "pcs_offline": "The Analysis PC with this software is not connected to the portal right now. Lab staff have been alerted; you keep your place.",
    "maintenance": "The Analysis PC with this software is under maintenance. You keep your place and start as soon as it is back.",
    "no_compatible_pc": "No Analysis PC has the software this equipment needs yet. Please contact the laboratory.",
    "allocating": "An Analysis PC is being assigned to you now.",
}


def required_names(reservation) -> tuple[str, ...]:
    caps = getattr(reservation, "requested_capabilities", None) or {}
    names = caps.get("required_software_names") or []
    if not names and getattr(reservation, "software_profile_id", None):
        profile = reservation.software_profile
        catalog = getattr(profile, "catalog_entry", None)
        name = (getattr(catalog, "name", "") or getattr(profile, "software", "") or "").strip()
        names = [name] if name else []
    return tuple(sorted({str(n).strip().lower() for n in names if str(n).strip()}))


class _Fleet:
    """Workstations and their installed software, loaded once per call."""

    def __init__(self):
        from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware

        self.workstations = {ws.id: ws for ws in AnalysisWorkstation.objects.filter(enabled=True)}
        self.software: dict[Any, list[str]] = {}
        for ws_id, name in InstalledSoftware.objects.filter(
            workstation_id__in=list(self.workstations), is_present=True, allocation_enabled=True
        ).values_list("workstation_id", "software_name"):
            self.software.setdefault(ws_id, []).append((name or "").lower())
        self._cache: dict[tuple[str, ...], frozenset] = {}

    def compatible(self, names: tuple[str, ...]) -> frozenset:
        if names not in self._cache:
            if not names:
                ids = frozenset(self.workstations)
            else:
                ids = frozenset(
                    ws_id
                    for ws_id in self.workstations
                    if all(any(want in have for have in self.software.get(ws_id, [])) for want in names)
                )
            self._cache[names] = ids
        return self._cache[names]


def _waiting_entries():
    from iic_booking.remote_analysis.scheduler_models import ReservationQueue

    return list(
        ReservationQueue.objects.filter(status__in=[QueueEntryStatus.WAITING, QueueEntryStatus.ALLOCATING])
        .select_related("reservation", "reservation__software_profile", "reservation__software_profile__catalog_entry")
        .order_by("priority", "enqueued_at", "id")
    )


def someone_waiting_for(workstation_id, *, exclude_reservation_id=None, fleet: _Fleet | None = None) -> bool:
    """True when a queued request could use this PC. Used to refuse extensions only when they would delay someone."""
    if workstation_id is None:
        return False
    fleet = fleet or _Fleet()
    for entry in _waiting_entries():
        if entry.reservation_id == exclude_reservation_id:
            continue
        if workstation_id in fleet.compatible(required_names(entry.reservation)):
            return True
    return False


def _free_at(ws, now: datetime, avg: timedelta, sessions: dict, holds: dict, online: bool) -> datetime | None:
    if not online or ws.status in UNUSABLE:
        return None
    session_end = sessions.get(ws.id)
    if session_end is not None:
        return max(now, session_end)
    if ws.id in holds:
        return now + avg  # held for someone who is about to start
    if ws.status in {WorkstationStatus.BUSY, WorkstationStatus.PREPARING, WorkstationStatus.RESERVED, WorkstationStatus.CLEANING}:
        return now + avg
    return now


def _round_up_minutes(delta: timedelta) -> int:
    minutes = max(0, math.ceil(delta.total_seconds() / 60))
    return 0 if minutes == 0 else max(5, int(math.ceil(minutes / 5.0) * 5))


def estimate_start(free_times: Iterable[datetime], ahead: int, avg: timedelta) -> datetime | None:
    heap = list(free_times)
    if not heap:
        return None
    heapq.heapify(heap)
    for _ in range(ahead):
        t = heapq.heappop(heap)
        heapq.heappush(heap, t + avg)
    return heap[0]


def insight(reservation, *, avg_session_minutes: int, now: datetime | None = None) -> dict[str, Any]:
    """Position among competing requests, estimated start, and a plain-language reason."""
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
    from iic_booking.remote_analysis.services.availability import AvailabilityEngine
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    now = now or timezone.now()
    avg = timedelta(minutes=max(5, int(avg_session_minutes or 30)))
    fleet = _Fleet()
    mine = fleet.compatible(required_names(reservation))
    entries = _waiting_entries()

    competing = [e for e in entries if fleet.compatible(required_names(e.reservation)) & mine]
    index = next((i for i, e in enumerate(competing) if e.reservation_id == reservation.id), None)
    if index is None:
        position = len(competing) + 1
        queue_size = position
    else:
        position = index + 1
        queue_size = len(competing)
    ahead = position - 1

    availability = AvailabilityEngine()
    sessions: dict = {}
    for ws_id, expires_at in RemoteDesktopSession.objects.filter(
        workstation_id__in=list(mine), status__in=OPEN_SESSION
    ).values_list("workstation_id", "expires_at"):
        end = expires_at or now + avg
        sessions[ws_id] = max(sessions.get(ws_id, end), end)
    holds = set(
        AnalysisReservation.objects.filter(
            workstation_id__in=list(mine), status=ReservationStatus.AWAITING_CHECKIN
        ).values_list("workstation_id", flat=True)
    )
    online_ids = {ws_id for ws_id in mine if availability.heartbeat_fresh(fleet.workstations[ws_id])}
    free_times = [
        t
        for ws_id in mine
        if (t := _free_at(fleet.workstations[ws_id], now, avg, sessions, holds, ws_id in online_ids)) is not None
    ]
    start = estimate_start(free_times, ahead, avg)

    if not mine:
        code = "no_compatible_pc"
    elif not free_times:
        code = "maintenance" if any(fleet.workstations[i].status in MAINTENANCE_LIKE for i in mine) else "pcs_offline"
    elif start is not None and start <= now:
        code = "allocating" if ahead == 0 else "ahead_in_queue"
    elif ahead:
        code = "ahead_in_queue"
    else:
        code = "pcs_busy"

    return {
        "position": position,
        "people_ahead": ahead,
        "queue_size": queue_size,
        "estimated_wait_minutes": _round_up_minutes(start - now) if start is not None else None,
        "expected_start_at": start.isoformat() if start is not None else None,
        "reason_code": code,
        "reason": REASONS[code],
        "compatible_pcs": len(mine),
        "compatible_pcs_online": len(online_ids),
        "average_session_minutes": int(avg.total_seconds() // 60),
    }
