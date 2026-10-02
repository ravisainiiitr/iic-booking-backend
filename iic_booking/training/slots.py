"""
Slot reservation for demos and training sessions.

Only free slots are taken: AVAILABLE with no booking. They become BLOCKED with a label and a
``SessionSlotReservation`` row remembering the previous status. BOOKED, HOLD (booked) and disrupted
slots are never touched, and nothing is refunded: if the window is not free the caller must choose
another one. Multi-mode families that share the physical instrument get the same block on their free
overlapping slots. Release restores a slot only while it is still blocked by this reservation.

Do not use the admin ``bulk-slot-status`` endpoint for this; it refunds bookings it blocks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, Equipment, SlotStatus

from .models import SessionSlotReservation, SessionStatus, TrainingSession

LABEL_MAX = 255


class ReservationError(Exception):
    def __init__(self, message: str, *, conflicts: list[dict] | None = None, code: str = "reservation_failed"):
        super().__init__(message)
        self.message = message
        self.conflicts = conflicts or []
        self.code = code


@dataclass
class ReservationResult:
    reserved_slot_ids: list[int] = field(default_factory=list)
    family_slot_ids: list[int] = field(default_factory=list)
    skipped_family: list[dict] = field(default_factory=list)


def _slot_info(slot: DailySlot) -> dict:
    return {
        "slot_id": slot.id,
        "equipment_id": slot.slot_master.equipment_id if slot.slot_master_id else None,
        "start": slot.start_datetime.isoformat() if slot.start_datetime else None,
        "end": slot.end_datetime.isoformat() if slot.end_datetime else None,
        "status": slot.status,
        "booked": bool(slot.booking_id),
        "label": slot.blocked_label or "",
    }


def _is_free(slot: DailySlot) -> bool:
    return slot.status == SlotStatus.AVAILABLE and not slot.booking_id


def demo_label(*, course: str, faculty_name: str) -> str:
    course = (course or "Demonstration").strip()
    who = (faculty_name or "").strip()
    if who and not who.lower().startswith(("prof", "dr")):
        who = f"Prof. {who}"
    text = f"Demo: {course}" + (f" ({who})" if who else "")
    return text[:LABEL_MAX]


def _family_ids(equipment: Equipment) -> list[int]:
    from iic_booking.equipment.mode_utils import mode_family_ids, multimode_enabled_for_equipment

    if not multimode_enabled_for_equipment(equipment):
        return []
    return [eid for eid in mode_family_ids(equipment) if eid != equipment.equipment_id]


def _window_slots(equipment_ids, start: datetime, end: datetime, *, lock: bool):
    qs = DailySlot.objects.filter(
        slot_master__equipment_id__in=list(equipment_ids),
        start_datetime__lt=end,
        end_datetime__gt=start,
    ).select_related("slot_master")
    if lock:
        qs = qs.select_for_update(of=("self",)) if _supports_select_for_update_of() else qs.select_for_update()
    return list(qs.order_by("start_datetime", "id"))


def _supports_select_for_update_of() -> bool:
    from django.db import connection

    return bool(getattr(connection.features, "has_select_for_update_of", False))


def _check_coverage(slots: list[DailySlot], start: datetime, end: datetime) -> None:
    if not slots:
        raise ReservationError(
            "No slots exist for that window yet. Slots are prepared weekly; choose a date in a generated week.",
            code="no_slots",
        )
    if slots[0].start_datetime > start or slots[-1].end_datetime < end:
        raise ReservationError("The window is not fully covered by the instrument's slots.", code="not_covered")
    for prev, nxt in zip(slots, slots[1:]):
        if nxt.start_datetime > prev.end_datetime:
            raise ReservationError("The window has a gap between slots.", code="not_contiguous")


def reserve_session_slots(
    session: TrainingSession, *, actor, label: str, start: datetime | None = None, end: datetime | None = None
) -> ReservationResult:
    equipment = session.equipment or session.event.equipment
    if equipment is None:
        raise ReservationError("This session has no equipment to reserve.", code="no_equipment")
    start = start or session.start_at
    end = end or session.end_at
    if not start or not end or end <= start:
        raise ReservationError("Invalid session window.", code="invalid_window")
    label = (label or "Training")[:LABEL_MAX]
    result = ReservationResult()
    with transaction.atomic():
        if session.slot_reservations.filter(released_at__isnull=True).exists():
            raise ReservationError("Slots are already reserved for this session; release them first.", code="already_reserved")
        slots = _window_slots([equipment.equipment_id], start, end, lock=True)
        _check_coverage(slots, start, end)
        busy = [s for s in slots if not _is_free(s)]
        if busy:
            raise ReservationError(
                "Some slots in that window are not free (booked, blocked or disrupted). Choose another window.",
                conflicts=[_slot_info(s) for s in busy],
                code="slots_not_free",
            )
        family_slots = []
        family_ids = _family_ids(equipment)
        if family_ids:
            family_slots = _window_slots(family_ids, start, end, lock=True)
            from iic_booking.equipment.mode_utils import requires_exclusive_family_conflict

            local = timezone.localtime(start)
            exclusive = requires_exclusive_family_conflict(equipment, local.date(), local.time())
            booked_family = [s for s in family_slots if s.booking_id]
            if exclusive and booked_family:
                raise ReservationError(
                    "Another mode of this instrument is booked in that window. Choose another window.",
                    conflicts=[_slot_info(s) for s in booked_family],
                    code="family_booked",
                )
        now = timezone.now()
        for slot in slots:
            _block(slot, session=session, equipment_id=equipment.equipment_id, label=label, actor=actor, family=False)
            result.reserved_slot_ids.append(slot.id)
        for slot in family_slots:
            if not _is_free(slot):
                result.skipped_family.append(_slot_info(slot))
                continue
            _block(slot, session=session, equipment_id=slot.slot_master.equipment_id, label=label, actor=actor, family=True)
            result.family_slot_ids.append(slot.id)
        TrainingSession.objects.filter(pk=session.pk).update(status=SessionStatus.SCHEDULED, updated_at=now)
        session.status = SessionStatus.SCHEDULED
    return result


def _block(slot: DailySlot, *, session, equipment_id: int, label: str, actor, family: bool) -> None:
    updated = DailySlot.objects.filter(pk=slot.pk, status=SlotStatus.AVAILABLE, booking__isnull=True).update(
        status=SlotStatus.BLOCKED, blocked_label=label, updated_at=timezone.now()
    )
    if updated != 1:
        # Lost a race with a booking between the read and the write (possible where row locks are no-ops).
        raise ReservationError(
            "A slot was taken while reserving. Nothing was reserved; please retry.",
            conflicts=[_slot_info(slot)],
            code="slot_race",
        )
    SessionSlotReservation.objects.create(
        session=session,
        daily_slot=slot,
        equipment_id=equipment_id,
        is_family_block=family,
        previous_status=slot.status,
        previous_label=slot.blocked_label,
        label=label,
        reserved_by=actor if getattr(actor, "pk", None) else None,
    )


def release_session_slots(session: TrainingSession, *, actor, note: str = "") -> dict:
    restored, left = [], []
    now = timezone.now()
    with transaction.atomic():
        reservations = list(
            session.slot_reservations.filter(released_at__isnull=True).select_related("daily_slot")
        )
        slot_ids = [r.daily_slot_id for r in reservations]
        locked = {
            s.id: s for s in DailySlot.objects.select_for_update().filter(id__in=slot_ids)
        } if slot_ids else {}
        for res in reservations:
            slot = locked.get(res.daily_slot_id)
            still_ours = (
                slot is not None
                and slot.status == SlotStatus.BLOCKED
                and not slot.booking_id
                and (slot.blocked_label or "") == (res.label or "")
            )
            if still_ours:
                DailySlot.objects.filter(pk=slot.pk).update(
                    status=res.previous_status, blocked_label=res.previous_label, updated_at=now
                )
                restored.append(slot.pk)
                res.release_note = note[:255]
            else:
                left.append(res.daily_slot_id)
                res.release_note = ("Slot changed after reservation; left as is. " + note)[:255]
            res.released_at = now
            res.released_by = actor if getattr(actor, "pk", None) else None
            res.save(update_fields=["released_at", "released_by", "release_note"])
        if session.status == SessionStatus.SCHEDULED:
            TrainingSession.objects.filter(pk=session.pk).update(status=SessionStatus.PLANNED, updated_at=now)
            session.status = SessionStatus.PLANNED
    return {"restored_slot_ids": restored, "left_unchanged_slot_ids": left}


def free_windows(equipment: Equipment, *, date_from, date_to, duration_minutes: int, limit: int = 200) -> list[dict]:
    """Start options where contiguous free slots cover ``duration_minutes``."""
    duration = timedelta(minutes=max(1, int(duration_minutes)))
    slots = list(
        DailySlot.objects.filter(
            slot_master__equipment=equipment, date__gte=date_from, date__lte=date_to
        ).order_by("start_datetime", "id")
    )
    now = timezone.now()
    windows: list[dict] = []
    for i, first in enumerate(slots):
        if not _is_free(first) or first.start_datetime <= now:
            continue
        covered = [first]
        end = first.end_datetime
        j = i
        while end - first.start_datetime < duration and j + 1 < len(slots):
            nxt = slots[j + 1]
            if nxt.start_datetime != end or not _is_free(nxt):
                break
            covered.append(nxt)
            end = nxt.end_datetime
            j += 1
        if end - first.start_datetime >= duration:
            windows.append(
                {
                    "start": first.start_datetime.isoformat(),
                    "end": (first.start_datetime + duration).isoformat(),
                    "slots_end": end.isoformat(),
                    "slot_ids": [s.id for s in covered],
                    "date": str(first.date),
                }
            )
            if len(windows) >= limit:
                break
    return windows


def window_status(equipment: Equipment, start: datetime, end: datetime) -> dict:
    """Read-only check of a window, for the inbox ('free' / '1 booked')."""
    slots = _window_slots([equipment.equipment_id], start, end, lock=False)
    if not slots:
        return {"state": "no_slots", "free": 0, "busy": 0}
    busy = [s for s in slots if not _is_free(s)]
    covered = slots[0].start_datetime <= start and slots[-1].end_datetime >= end
    return {
        "state": "free" if not busy and covered else ("partial" if not covered else "busy"),
        "free": len(slots) - len(busy),
        "busy": len(busy),
        "booked": sum(1 for s in busy if s.booking_id),
    }
