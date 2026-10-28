"""
Bookings on hold for equipment maintenance are moved automatically, fairly, once the equipment works again.

``allocate_for_equipment`` runs inside the transaction that marks the equipment Operational (after
``maintenance_auto.end_period`` has put the converted slots back), so nobody else can see or book the freed slots
before the bookings on hold are placed; it runs again at the 08:30 check for bookings that did not fit.

* Order: original slot start, then when the booking was made (then booking id).
* Each booking gets the earliest run of consecutive Available slots with the same number of slots and the same
  total length, starting at least ``min_lead_hours`` from now and never the same day, on a working day (no weekend
  or institute holiday), allowed for the equipment mode at that time, within ``horizon_days`` (weeks not yet open to
  users included; missing slots are generated first). A booking spanning several days (more slots than a day has)
  is placed day by day: each day's block of slots on its own later working day, the full booking again.
* The booking keeps its price and quota period, becomes Booked (Pending when the lab already had the sample), and
  records a history entry by the System; the user gets an email and an in-app notice, staff a summary.
* A booking that does not fit stays on hold and is retried at the next 08:30 check; staff are told.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Optional

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

RESCHEDULE_REASON = "the equipment was under maintenance"


@dataclass
class AllocationResult:
    placed: list = field(default_factory=list)
    unplaced: list = field(default_factory=list)


def settings_row():
    from .models import MaintenanceRescheduleSettings

    return MaintenanceRescheduleSettings.current()


def pending_holds(equipment_id):
    from .models import Booking, BookingStatus

    return Booking.objects.filter(
        equipment_id=equipment_id,
        status=BookingStatus.DISRUPTION_PENDING,
        maintenance_auto_reschedule_pending=True,
    )


def earliest_new_start(now, cfg) -> datetime:
    tz = timezone.get_current_timezone()
    tomorrow = timezone.make_aware(datetime.combine(timezone.localtime(now).date() + timedelta(days=1), time.min), tz)
    return max(now + timedelta(hours=int(cfg.min_lead_hours or 0)), tomorrow)


def _iso(dt) -> str:
    return timezone.localtime(dt).isoformat(timespec="minutes")


def _slot_minutes(slot) -> int:
    if not slot.start_datetime or not slot.end_datetime:
        return 0
    return int((slot.end_datetime - slot.start_datetime).total_seconds() // 60)


def _held_slots(booking) -> list:
    return sorted(
        [s for s in booking.daily_slots.all() if s.start_datetime],
        key=lambda s: s.start_datetime,
    )


def _order_key(booking):
    slots = _held_slots(booking)
    first = slots[0].start_datetime if slots else None
    return (first is None, first or booking.created_at, booking.created_at, booking.booking_id)


def _mode_allows(equipment, slot) -> bool:
    from .mode_utils import equipment_bookable_on_date, family_slots_overlap_conflict

    local = timezone.localtime(slot.start_datetime)
    ok, _msg = equipment_bookable_on_date(equipment, local.date(), local.time())
    if not ok:
        return False
    return family_slots_overlap_conflict(equipment, slot.start_datetime, slot.end_datetime) is None


def _free_slots(equipment, earliest, until_date, *, lock: bool = True):
    from .models import DailySlot, Holiday, SlotStatus

    holidays = set(Holiday.get_holidays_in_range(timezone.localtime(earliest).date(), until_date).keys())
    base = DailySlot.objects.select_for_update(of=("self",)) if lock else DailySlot.objects.all()
    slots = (
        base.filter(
            slot_master__equipment_id=equipment.pk,
            status=SlotStatus.AVAILABLE,
            booking__isnull=True,
            start_datetime__gte=earliest,
            date__lte=until_date,
        )
        .order_by("start_datetime", "id")
    )
    return [s for s in slots if s.end_datetime and s.date not in holidays]


def find_run(free: list, count: int, minutes: int, equipment, taken: set) -> Optional[list]:
    """Earliest run of ``count`` back-to-back free slots totalling ``minutes`` (any length when 0)."""
    if count <= 0:
        return None
    for i in range(len(free)):
        run = [free[i]]
        if free[i].pk in taken:
            continue
        j = i + 1
        while len(run) < count and j < len(free):
            nxt = free[j]
            if nxt.pk in taken or nxt.start_datetime != run[-1].end_datetime:
                break
            run.append(nxt)
            j += 1
        if len(run) != count:
            continue
        if minutes and sum(_slot_minutes(s) for s in run) != minutes:
            continue
        if all(_mode_allows(equipment, s) for s in run):
            return run
    return None


def _day_blocks(held: list) -> list[tuple[int, int]]:
    """The booking's slots per calendar day, in order: ``[(slot count, total minutes), ...]``."""
    blocks: list[tuple[int, int]] = []
    last_day = None
    for s in held:
        day = timezone.localtime(s.start_datetime).date()
        if day != last_day:
            blocks.append((0, 0))
            last_day = day
        count, minutes = blocks[-1]
        blocks[-1] = (count + 1, minutes + _slot_minutes(s))
    return blocks


def find_placement(free: list, held: list, equipment, taken: set) -> Optional[list]:
    """Where a booking on hold goes: the same shape as its slots.

    One day: the earliest run of back-to-back slots (``find_run``). Several days (a booking longer than a day's
    slots, e.g. 16 + 5): each day's block on its own later working day, earliest first, the full booking again.
    """
    blocks = _day_blocks(held)
    if len(blocks) <= 1:
        return find_run(free, len(held), sum(_slot_minutes(s) for s in held), equipment, taken)
    placed: list = []
    used = set(taken)
    after_day = None
    for count, minutes in blocks:
        candidates = free if after_day is None else [
            s for s in free if timezone.localtime(s.start_datetime).date() > after_day
        ]
        run = find_run(candidates, count, minutes, equipment, used)
        if run is None:
            return None
        day = timezone.localtime(run[0].start_datetime).date()
        if any(timezone.localtime(s.start_datetime).date() != day for s in run):
            return None  # a day's block stays within one day
        placed.extend(run)
        used.update(s.pk for s in run)
        after_day = day
    return placed


def _status_before_hold(booking):
    from .models import BookingEvent, BookingStatus

    prev = (
        BookingEvent.objects.filter(booking=booking, new_status=BookingStatus.DISRUPTION_PENDING)
        .order_by("-created_at", "-event_id")
        .values_list("previous_status", flat=True)
        .first()
    )
    return BookingStatus.PENDING if prev == BookingStatus.PENDING else BookingStatus.BOOKED


def _move(booking, run: list, now, cfg) -> dict:
    from .booking_events import create_booking_event
    from .maintenance_policy import clear_disruption_policy_fields, released_slot_status_after_booking_freed
    from .models import BookingEventType, DailySlot, SlotStatus

    old = _held_slots(booking)
    original_start = old[0].start_datetime if old else None
    future_free = released_slot_status_after_booking_freed(booking.equipment)
    for slot in old:
        slot.booking = None
        slot.status = future_free if slot.start_datetime > now else SlotStatus.UNDER_MAINTENANCE
        slot.save(update_fields=["booking", "status"])
    DailySlot.objects.filter(pk__in=[s.pk for s in run]).update(booking=booking, status=SlotStatus.BOOKED)

    previous_status = booking.status
    booking.status = _status_before_hold(booking)
    clear_disruption_policy_fields(booking, keep_quota_anchor=True)
    booking.auto_rescheduled_at = now
    booking.auto_rescheduled_from = original_start
    booking.auto_reschedule_extension_hours = int(cfg.deadline_extension_hours or 0)
    booking.sample_submission_deadline_reminder_sent_at = None
    booking.save()

    from .maintenance_auto import _fmt

    original_text = _fmt(original_start) if original_start else "—"
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.RESCHEDULED,
        previous_status=previous_status,
        new_status=booking.status,
        comment=(
            f"Auto-rescheduled after maintenance (originally {original_text}) to "
            f"{_fmt(run[0].start_datetime)} – {timezone.localtime(run[-1].end_datetime):%I:%M %p}."
        ),
        created_by=None,
        system_actor=True,
        send_notification=False,
        metadata={
            "maintenance_auto_reschedule": True,
            "original_start": original_start.isoformat() if original_start else None,
            "new_start": run[0].start_datetime.isoformat(),
            "new_end": run[-1].end_datetime.isoformat(),
            "released_slot_ids": [s.pk for s in old],
            "booked_slot_ids": [s.pk for s in run],
            "deadline_extension_hours": booking.auto_reschedule_extension_hours,
        },
    )
    return {"booking": booking, "original_start": original_start}


def allocate_for_equipment(equipment, *, now=None) -> AllocationResult:
    """Place every booking on hold for this equipment's maintenance (see the module docstring). Idempotent."""
    from .maintenance_policy import is_equipment_operational_status
    from .models import Equipment
    from .slot_utils import SlotGenerator

    now = now or timezone.now()
    result = AllocationResult()
    with transaction.atomic():
        equipment = Equipment.objects.select_for_update().filter(pk=equipment.pk).first()
        if equipment is None or not is_equipment_operational_status(equipment.status):
            return result
        holds = list(
            pending_holds(equipment.pk)
            .select_for_update(of=("self",))
            .select_related("user", "equipment")
            .prefetch_related("daily_slots")
        )
        if not holds:
            return result
        holds.sort(key=_order_key)
        cfg = settings_row()
        earliest = earliest_new_start(now, cfg)
        until = timezone.localtime(now).date() + timedelta(days=int(cfg.horizon_days or 0))
        try:
            SlotGenerator.generate_weekly_slots(equipment, timezone.localtime(earliest).date(), until)
        except Exception:
            logger.exception("maintenance_reschedule: slot generation failed for equipment %s", equipment.pk)
        free = _free_slots(equipment, earliest, until)
        taken: set = set()
        for booking in holds:
            held = _held_slots(booking)
            run = find_placement(free, held, equipment, taken)
            if run is None:
                result.unplaced.append(booking)
                continue
            moved = _move(booking, run, now, cfg)
            taken.update(s.pk for s in run)
            result.placed.append(moved)
        _notify(equipment, result, now, cfg)
    logger.info(
        "maintenance_reschedule: equipment %s placed=%s unplaced=%s",
        equipment.pk, len(result.placed), len(result.unplaced),
    )
    return result


# --- deadlines ----------------------------------------------------------------------------------------------------


def extension_hours(booking) -> int:
    if not getattr(booking, "auto_rescheduled_at", None):
        return 0
    return int(getattr(booking, "auto_reschedule_extension_hours", 0) or 0)


def cancel_full_refund_until(booking) -> Optional[datetime]:
    """Auto-rescheduled bookings: the normal cancellation cutoff of the new slot, or ``cancel_grace_hours`` after
    the reschedule when that is later; never after the slot start. None for other bookings."""
    at = getattr(booking, "auto_rescheduled_at", None)
    if not at:
        return None
    from .serializers import _booking_slot_bounds

    start, _end = _booking_slot_bounds(booking)
    if start is None:
        return None
    threshold = getattr(booking.equipment, "reschedule_hours_threshold", None) or 48
    normal = start - timedelta(hours=int(threshold))
    grace = at + timedelta(hours=int(settings_row().cancel_grace_hours or 0))
    return min(max(normal, grace), start)


def auto_reschedule_payload(booking) -> Optional[dict]:
    """Badge / note for staff and the user: originally when, and the extended sample deadline."""
    at = getattr(booking, "auto_rescheduled_at", None)
    if not at:
        return None
    from .maintenance_auto import _fmt
    from .sample_submission_deadline_reminders import compute_sample_submission_deadline

    original = getattr(booking, "auto_rescheduled_from", None)
    try:
        deadline = compute_sample_submission_deadline(booking)
    except Exception:
        deadline = None
    cancel_until = cancel_full_refund_until(booking)
    return {
        "rescheduled_at": at.isoformat(),
        "original_start": original.isoformat() if original else None,
        "original_display": _fmt(original) if original else "",
        "sample_deadline": deadline.isoformat() if deadline else None,
        "sample_deadline_display": _fmt(deadline) if deadline else "",
        "extension_hours": extension_hours(booking),
        "cancel_full_refund_until": cancel_until.isoformat() if cancel_until else None,
        "note": (
            f"Auto-rescheduled after maintenance — originally {_fmt(original) if original else '—'}"
            + (f"; sample deadline extended to {_fmt(deadline)}" if deadline else "")
        ),
    }


# --- notifications ------------------------------------------------------------------------------------------------


def _latest_period(equipment):
    from .models import EquipmentMaintenancePeriod

    return EquipmentMaintenancePeriod.objects.filter(equipment_id=equipment.pk).order_by("-started_at", "-id").first()


def _notify(equipment, result: AllocationResult, now, cfg) -> None:
    from .booking_links import booking_detail_path
    from .maintenance_auto import (
        TEMPLATE_RESCHEDULED,
        _booking_ref,
        _claim,
        _deliver,
        _equipment_label,
        _fmt,
        _staff_summary,
    )

    period = _latest_period(equipment)
    eq = _equipment_label(equipment)
    for item in result.placed:
        booking = item["booking"]
        if period is not None and not _claim(period, "RESCHEDULED", booking=booking, user=booking.user):
            continue
        booking.refresh_from_db()
        payload = auto_reschedule_payload(booking) or {}
        from .serializers import _booking_slot_bounds

        start, end = _booking_slot_bounds(booking)
        ref = _booking_ref(booking)
        cancel_until = cancel_full_refund_until(booking)
        _deliver(
            booking.user,
            TEMPLATE_RESCHEDULED,
            {
                "equipment_name": eq,
                "equipment_code": getattr(equipment, "code", "") or "",
                "booking_id": ref,
                "original_start": payload.get("original_display", ""),
                "start_time": _fmt(start),
                "end_time": _fmt(end),
                "reason": RESCHEDULE_REASON,
                "sample_deadline": payload.get("sample_deadline_display", ""),
                "cancel_until": _fmt(cancel_until) if cancel_until else "",
            },
            title=f"Booking {ref} rescheduled: {eq}",
            message=(
                f"{eq} is working again. Booking {ref} was moved automatically to {_fmt(start)} "
                f"(originally {payload.get('original_display') or '—'}), at no extra charge."
            ),
            path=booking_detail_path(display_id=ref, staff=False) or "/my-bookings",
            event="maintenance.rescheduled",
            meta={"booking_id": booking.booking_id, **({"maintenance_period_id": period.pk} if period else {})},
        )
    if period is None:
        return
    today = timezone.localtime(now).date()
    placed = [item["booking"] for item in result.placed]
    if placed:
        _staff_summary(
            period,
            "STAFF_RESCHEDULED",
            subject=f"Rescheduled automatically: {eq} – {len(placed)} booking{'s' if len(placed) != 1 else ''}",
            intro=(
                f"{eq} is Operational. The bookings that were on hold for the maintenance were moved, in booking "
                f"order, to the earliest free slots (at least {cfg.min_lead_hours} h ahead, never the same day). "
                f"Their sample deadline and Not Utilized threshold are extended by {cfg.deadline_extension_hours} h; "
                "the users were emailed."
            ),
            bookings=placed,
            for_date=today,
            extra={"bookings_rescheduled": len(placed), "bookings_waiting": len(result.unplaced) or ""},
            claim_booking=placed[0],
        )
    if result.unplaced:
        _staff_summary(
            period,
            "STAFF_UNPLACED",
            subject=(
                f"No free slot yet: {eq} – {len(result.unplaced)} booking"
                f"{'s' if len(result.unplaced) != 1 else ''} still on hold"
            ),
            intro=(
                f"No free run of slots was found within {cfg.horizon_days} days for these bookings on hold after "
                f"the maintenance of {eq}. They stay on hold (the users may still cancel for a full refund) and are "
                "retried every morning at 8:30 AM. Opening more slots, or rescheduling them by hand, places them."
            ),
            bookings=result.unplaced,
            for_date=today,
            extra={"bookings_waiting": len(result.unplaced)},
        )


def allocate_pending_everywhere(now=None) -> dict:
    """08:30: Operational equipment that still has bookings on hold for maintenance."""
    from .maintenance_policy import is_equipment_operational_status
    from .models import Booking, BookingStatus, Equipment

    now = now or timezone.now()
    ids = sorted(
        set(
            Booking.objects.filter(
                status=BookingStatus.DISRUPTION_PENDING, maintenance_auto_reschedule_pending=True
            ).values_list("equipment_id", flat=True)
        )
    )
    stats = {"equipment": 0, "placed": 0, "unplaced": 0}
    for eq in Equipment.objects.filter(pk__in=ids):
        if not is_equipment_operational_status(eq.status):
            continue
        stats["equipment"] += 1
        try:
            res = allocate_for_equipment(eq, now=now)
        except Exception:
            logger.exception("maintenance_reschedule: allocation failed for equipment %s", eq.pk)
            continue
        stats["placed"] += len(res.placed)
        stats["unplaced"] += len(res.unplaced)
    return stats


def allocation_preview(equipment, now=None, *, include_ids=()) -> dict:
    """Read-only: ids of the bookings on hold and where they would go now among today's Available slots (slots that
    would come back from Under Maintenance are not counted, so this is a lower bound while under maintenance).
    ``include_ids``: maintenance holds not yet switched to automatic rescheduling, previewed as if they were."""
    from django.db.models import Q

    from .models import Booking, BookingStatus

    now = now or timezone.now()
    holds_qs = Booking.objects.filter(
        Q(pk__in=pending_holds(equipment.pk).values("pk"))
        | Q(pk__in=list(include_ids), equipment_id=equipment.pk, status=BookingStatus.DISRUPTION_PENDING)
    )
    holds = sorted(holds_qs.select_related("equipment").prefetch_related("daily_slots"), key=_order_key)
    cfg = settings_row()
    earliest = earliest_new_start(now, cfg)
    until = timezone.localtime(now).date() + timedelta(days=int(cfg.horizon_days or 0))
    free = _free_slots(equipment, earliest, until, lock=False)
    taken: set = set()
    rows = []
    for b in holds:
        held = _held_slots(b)
        run = find_placement(free, held, equipment, taken) if free else None
        if run:
            taken.update(s.pk for s in run)
        rows.append(
            {
                "booking_id": b.booking_id,
                "slots": len(held),
                "original_start": _iso(held[0].start_datetime) if held else None,
                "new_start": _iso(run[0].start_datetime) if run else None,
                "new_end": _iso(run[-1].end_datetime) if run else None,
            }
        )
    return {"equipment_id": equipment.pk, "holds": len(holds), "rows": rows}


# --- switching earlier maintenance holds to automatic rescheduling (admin-approved, one-off) ----------------------

HOLD_UPDATE_MARK = "hold_update_notice"
_WHEN_BACK = "once the equipment is back in working condition"
_WHEN_SLOT = "as soon as a matching free slot is available"


def _hold_update_sent(booking) -> bool:
    from .models import BookingEvent

    return BookingEvent.objects.filter(booking=booking, **{f"metadata__{HOLD_UPDATE_MARK}": True}).exists()


def hold_update_review(booking_ids, now=None) -> list[dict]:
    """Read-only: for each booking, whether it is a maintenance hold still awaiting the user (no refund / reschedule
    chosen) and what switching it to automatic rescheduling would do."""
    from .maintenance_policy import is_equipment_operational_status, is_equipment_under_maintenance_status
    from .models import Booking, BookingDisruptionKind, BookingStatus

    now = now or timezone.now()
    ids = [int(i) for i in booking_ids]
    found = {
        b.pk: b
        for b in Booking.objects.filter(pk__in=ids).select_related("equipment").prefetch_related("daily_slots")
    }
    rows = []
    for bid in ids:
        b = found.get(bid)
        row = {"booking_id": bid, "action": None, "skip": None}
        rows.append(row)
        if b is None:
            row["skip"] = "booking not found"
            continue
        eq = b.equipment
        row.update(
            equipment_id=eq.pk,
            equipment_code=eq.code,
            equipment_status=eq.status,
            status=b.status,
            kind=b.disruption_kind,
            pending=bool(b.maintenance_auto_reschedule_pending),
            deadline_set=b.maintenance_decision_deadline_at is not None,
            slots=len(_held_slots(b)),
            notice_sent=_hold_update_sent(b),
        )
        if b.status != BookingStatus.DISRUPTION_PENDING:
            row["skip"] = f"status {b.status}: the user already chose, or the booking is closed"
        elif b.disruption_kind != BookingDisruptionKind.MAINTENANCE:
            row["skip"] = f"disruption kind {b.disruption_kind}, not equipment maintenance"
        elif not row["slots"]:
            row["skip"] = "no slots linked to the booking, so a new slot of the same length cannot be found"
        elif is_equipment_under_maintenance_status(eq.status):
            row["action"] = "notify" if row["pending"] else "convert_notify"
        elif is_equipment_operational_status(eq.status):
            row["action"] = "convert_reschedule"
        else:
            row["skip"] = f"equipment status {eq.status} (neither under maintenance nor operational)"
        if row["action"] in ("notify", "convert_notify") and row["notice_sent"]:
            row["action"] = "convert" if row["action"] == "convert_notify" else "none"
    by_eq: dict = {}
    for row in rows:
        if row["action"] == "convert_reschedule":
            by_eq.setdefault(row["equipment_id"], []).append(row)
    for eq_id, eq_rows in by_eq.items():
        eq = found[eq_rows[0]["booking_id"]].equipment
        preview = allocation_preview(eq, now, include_ids=[r["booking_id"] for r in eq_rows])
        where = {p["booking_id"]: p for p in preview["rows"]}
        for row in eq_rows:
            p = where.get(row["booking_id"]) or {}
            row["original_start"] = p.get("original_start")
            row["new_start"] = p.get("new_start")
            row["new_end"] = p.get("new_end")
    return rows


def _send_hold_update(booking, *, when: str) -> None:
    from .booking_events import create_booking_event
    from .booking_links import booking_detail_path
    from .maintenance_auto import TEMPLATE_HOLD_UPDATE, _booking_ref, _booking_window, _deliver, _equipment_label, _fmt
    from .models import BookingEventType

    period = _latest_period(booking.equipment)
    first, last = _booking_window(booking)
    ref = _booking_ref(booking)
    eq = _equipment_label(booking.equipment)
    _deliver(
        booking.user,
        TEMPLATE_HOLD_UPDATE,
        {
            "equipment_name": eq,
            "equipment_code": getattr(booking.equipment, "code", "") or "",
            "booking_id": ref,
            "start_time": _fmt(first) if first else "",
            "end_time": _fmt(last) if last else "",
            "maintenance_since": _fmt(period.started_at) if period and not period.ended_at else "",
            "reschedule_when": when,
        },
        title=f"Update: booking {ref} will be rescheduled automatically",
        message=(
            f"No choice is needed and there is no deadline: booking {ref} on {eq} is moved automatically to the "
            f"earliest free slot {when}. You can cancel it at any time for a full refund until then."
        ),
        path=booking_detail_path(display_id=ref, staff=False) or "/my-bookings",
        event="maintenance.hold_update",
        meta={"booking_id": booking.booking_id},
    )
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_by=None,
        system_actor=True,
        comment=(
            "User emailed: no decision deadline; the booking will be rescheduled automatically and can be cancelled "
            "for a full refund until then."
        ),
        metadata={"maintenance_auto_reschedule": True, HOLD_UPDATE_MARK: True},
        send_notification=False,
    )


def _convert(booking) -> None:
    from .booking_events import create_booking_event
    from .models import BookingEventType

    booking.maintenance_auto_reschedule_pending = True
    booking.maintenance_decision_deadline_at = None
    booking.save(update_fields=["maintenance_auto_reschedule_pending", "maintenance_decision_deadline_at"])
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_by=None,
        system_actor=True,
        comment=(
            "Maintenance hold policy updated: no decision deadline and no automatic refund. The booking is moved "
            "automatically to the earliest free slot (in booking order) once the equipment is Operational; the user "
            "may cancel for a full refund until then."
        ),
        metadata={"maintenance_auto_reschedule": True, "hold_policy_update": True},
        send_notification=False,
    )


def apply_hold_update(booking_ids, now=None) -> dict:
    """Switch the given maintenance holds to automatic rescheduling and tell their users once. Holds on equipment
    already Operational are placed right away (the reschedule email is their notice). Idempotent."""
    from .models import Booking, Equipment

    now = now or timezone.now()
    out = {"converted": [], "notified": [], "rescheduled": [], "still_on_hold": [], "skipped": {}}
    to_place: dict = {}
    with transaction.atomic():
        list(Booking.objects.select_for_update().filter(pk__in=list(booking_ids)).values_list("pk", flat=True))
        for row in hold_update_review(booking_ids, now):
            bid = row["booking_id"]
            if row["skip"]:
                out["skipped"][bid] = row["skip"]
                continue
            action = row["action"]
            if action == "none":
                out["skipped"][bid] = "already switched and notified"
                continue
            booking = Booking.objects.select_related("equipment", "user").get(pk=bid)
            if action in ("convert", "convert_notify", "convert_reschedule"):
                _convert(booking)
                out["converted"].append(bid)
            if action in ("notify", "convert_notify"):
                _send_hold_update(booking, when=_WHEN_BACK)
                out["notified"].append(bid)
            if action == "convert_reschedule":
                to_place.setdefault(row["equipment_id"], []).append(bid)
    for eq_id, ids in to_place.items():
        res = allocate_for_equipment(Equipment.objects.get(pk=eq_id), now=now)
        from .serializers import _booking_slot_bounds

        for item in res.placed:
            b = item["booking"]
            start, end = _booking_slot_bounds(b)
            out["rescheduled"].append(
                {
                    "booking_id": b.booking_id,
                    "requested": b.booking_id in ids,
                    "original_start": _iso(item["original_start"]) if item["original_start"] else None,
                    "new_start": _iso(start) if start else None,
                    "new_end": _iso(end) if end else None,
                }
            )
        placed = {item["booking"].booking_id for item in res.placed}
        for bid in ids:
            if bid in placed:
                continue
            out["still_on_hold"].append(bid)
            with transaction.atomic():
                booking = Booking.objects.select_for_update().select_related("equipment", "user").get(pk=bid)
                if not _hold_update_sent(booking):
                    _send_hold_update(booking, when=_WHEN_SLOT)
                    out["notified"].append(bid)
    return out
