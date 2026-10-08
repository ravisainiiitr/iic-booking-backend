"""Record disruption events and slot status changes from every path that changes slot or equipment status.

Rules
- Under Maintenance, Operator Absent, Scheduled Maintenance and Other Reasons (BLOCKED set by staff) are
  disruptions. Not Available, Reserved (External), Booking Not Utilized and non-home reservations are not.
- Slots marked together are grouped into runs: two slots belong to the same run unless a slot between them is
  usable time (Available, Booked, Reserved (External), Booking Not Utilized) or a different disruption.
  Closed time between them (Not Available, holidays, Other Reasons blocks) does not split a run.
- A run that touches an open event of the same type on the same equipment extends that event, unless both
  carry a different reason (then it is recorded separately).
- Slots that change to any other status are released from their event; an event closes when staff resume it
  and none of its slots are still waiting to start. Slot events whose last slot has ended count as closed.
- Whole-equipment Under Maintenance opens one event; it closes when the equipment is Operational again.
Recording never blocks the status change itself: failures are logged.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

logger = logging.getLogger(__name__)

REASON_MAX_LENGTH = 2000
EXTERNAL_REFERENCE_MAX_LENGTH = 100

REASON_CATEGORIES = {
    "UNDER_MAINTENANCE": [
        ("BREAKDOWN", "Breakdown"),
        ("CALIBRATION", "Calibration"),
        ("CONSUMABLES", "Consumables / spares"),
        ("UTILITIES", "Utilities / power"),
        ("SOFTWARE", "Software / computer"),
        ("OTHER", "Other"),
    ],
    "SCHEDULED_MAINTENANCE": [
        ("PREVENTIVE", "Preventive maintenance"),
        ("CALIBRATION", "Calibration"),
        ("AMC_VISIT", "AMC / service visit"),
        ("UPGRADE", "Upgrade / installation"),
        ("OTHER", "Other"),
    ],
    "OPERATOR_ABSENT": [
        ("LEAVE", "Leave"),
        ("TRAINING", "Training"),
        ("ILLNESS", "Illness"),
        ("OFFICIAL_DUTY", "Official duty"),
        ("OTHER", "Other"),
    ],
    "OTHER": [
        ("UTILITIES", "Utilities / power"),
        ("SAMPLE_ISSUE", "Sample / consumable issue"),
        ("SAFETY", "Safety"),
        ("ADMINISTRATIVE", "Administrative"),
        ("OTHER", "Other"),
    ],
}


def _slot_status():
    from .models import SlotStatus

    return SlotStatus


def disruption_type_for_slot_status(status: str | None) -> str | None:
    from .models import DisruptionType

    SlotStatus = _slot_status()
    return {
        SlotStatus.UNDER_MAINTENANCE: DisruptionType.UNDER_MAINTENANCE,
        SlotStatus.OPERATOR_ABSENT: DisruptionType.OPERATOR_ABSENT,
        SlotStatus.SCHEDULED_MAINTENANCE: DisruptionType.SCHEDULED_MAINTENANCE,
        SlotStatus.BLOCKED: DisruptionType.OTHER,
    }.get((status or "").strip().upper())


def _usable_statuses() -> set[str]:
    SlotStatus = _slot_status()
    return {
        SlotStatus.AVAILABLE,
        SlotStatus.BOOKED,
        SlotStatus.BOOKING_NOT_UTILIZED,
        SlotStatus.RESERVED_EXTERNAL,
    }


def clean_reason_category(disruption_type: str | None, raw) -> str:
    value = str(raw or "").strip().upper()
    keys = {k for k, _ in REASON_CATEGORIES.get(disruption_type or "", [])}
    return value if value in keys else ""


def reason_category_label(disruption_type: str | None, key: str | None) -> str:
    for k, label in REASON_CATEGORIES.get(disruption_type or "", []):
        if k == key:
            return label
    return ""


def clean_text(raw, limit: int = REASON_MAX_LENGTH) -> str:
    return str(raw or "").replace("\x00", "").strip()[:limit]


@dataclass
class DisruptionInput:
    user: object | None = None
    source: str = "OTHER"
    reason: str = ""
    reason_category: str = ""
    action_taken: str = ""
    label: str = ""
    external_reference: str = ""

    @classmethod
    def from_request_data(cls, data, *, user, source: str) -> "DisruptionInput":
        data = data or {}
        return cls(
            user=user,
            source=source,
            reason=clean_text(data.get("disruption_reason")),
            reason_category=str(data.get("disruption_reason_category") or "").strip().upper(),
            action_taken=clean_text(data.get("resolution_action")),
            external_reference=clean_text(data.get("external_reference"), EXTERNAL_REFERENCE_MAX_LENGTH),
        )


@dataclass
class RecordResult:
    opened: list[int] = field(default_factory=list)
    extended: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    resumed: list[int] = field(default_factory=list)
    log_id: int | None = None

    def as_dict(self) -> dict:
        return {
            "opened": self.opened,
            "extended": self.extended,
            "closed": self.closed,
            "resumed": self.resumed,
        }


def _user_or_none(user):
    return user if user is not None and getattr(user, "is_authenticated", False) and getattr(user, "pk", None) else None


def _log_edit(event, kind: str, user=None, *, field_name: str = "", old: str = "", new: str = "", note: str = ""):
    from .models import DisruptionEventEdit

    DisruptionEventEdit.objects.create(
        event=event,
        kind=kind,
        field=field_name,
        old_value=old or "",
        new_value=new or "",
        note=(note or "")[:255],
        edited_by=_user_or_none(user),
    )


# ---------------------------------------------------------------------------
# Status / duration helpers (shared with the API and reports)
# ---------------------------------------------------------------------------


def active_events_q(now=None) -> Q:
    """Events still in progress: not resumed, and (whole equipment, or a slot still to end)."""
    from .models import DisruptionScope

    now = now or timezone.now()
    return Q(ended_at__isnull=True) & (
        Q(scope=DisruptionScope.EQUIPMENT) | Q(end_at__isnull=True) | Q(end_at__gt=now)
    )


def event_is_open(event, now=None) -> bool:
    from .models import DisruptionScope

    now = now or timezone.now()
    if event.ended_at is not None:
        return False
    if event.scope == DisruptionScope.EQUIPMENT:
        return True
    return event.end_at is None or event.end_at > now


def _link_effective_end(link):
    if link.released_at is None:
        return link.end_datetime
    if link.released_at <= link.start_datetime:
        return None
    return min(link.end_datetime, link.released_at)


def event_duration_hours(event, links=None, now=None) -> float:
    from .models import DisruptionScope

    now = now or timezone.now()
    if event.scope == DisruptionScope.EQUIPMENT:
        end = event.ended_at or now
        return max(0.0, (end - event.start_at).total_seconds() / 3600.0)
    links = list(links if links is not None else event.slot_links.all())
    seconds = 0.0
    for link in links:
        end = _link_effective_end(link)
        if end is not None and end > link.start_datetime:
            seconds += (end - link.start_datetime).total_seconds()
    return max(0.0, seconds / 3600.0)


def _refresh_event_span(event) -> None:
    links = list(event.slot_links.all())
    if not links:
        return
    ends = [e for e in (_link_effective_end(link) for link in links) if e is not None]
    event.start_at = min(link.start_datetime for link in links)
    event.end_at = max(ends) if ends else event.start_at
    event.slots_affected = sum(1 for link in links if _link_effective_end(link) is not None)


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def _group_runs(equipment, selected: list[dict], disruption_type: str) -> list[list[dict]]:
    """Split ``selected`` (dicts with id/start/end) into runs; see module docstring for what splits a run."""
    if not selected:
        return []
    from .models import DailySlot

    selected = sorted(selected, key=lambda r: (r["start"], r["id"]))
    selected_ids = {r["id"] for r in selected}
    lo, hi = selected[0]["start"], selected[-1]["start"]
    between = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        start_datetime__gt=lo,
        start_datetime__lt=hi,
    ).values_list("id", "start_datetime", "status")
    usable = _usable_statuses()
    breakers = sorted(
        start
        for sid, start, st in between
        if sid not in selected_ids
        and (st in usable or (disruption_type_for_slot_status(st) not in (None, disruption_type, "OTHER")))
    )
    runs: list[list[dict]] = [[selected[0]]]
    bi = 0
    for row in selected[1:]:
        prev = runs[-1][-1]
        while bi < len(breakers) and breakers[bi] <= prev["start"]:
            bi += 1
        if bi < len(breakers) and breakers[bi] < row["start"]:
            runs.append([row])
        else:
            runs[-1].append(row)
    return runs


def _slot_rows(slots) -> list[dict]:
    rows = []
    for s in slots:
        if not getattr(s, "start_datetime", None) or not getattr(s, "end_datetime", None):
            continue
        rows.append(
            {
                "id": s.pk,
                "start": s.start_datetime,
                "end": s.end_datetime,
                "booking_id": getattr(s, "booking_id", None),
                "old_status": getattr(s, "_disruption_old_status", None) or getattr(s, "status", None),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Slot paths
# ---------------------------------------------------------------------------


def _release_links(slot_ids, *, keep_type: str | None, data: DisruptionInput, now, result: RecordResult,
                   resume: bool) -> None:
    """Release unreleased links of ``slot_ids`` to events of another type; close events with nothing left to come."""
    from .models import DisruptionEvent, DisruptionEventSlot, DisruptionScope

    links = DisruptionEventSlot.objects.filter(daily_slot_id__in=slot_ids, released_at__isnull=True).select_related(
        "event"
    )
    if keep_type:
        links = links.exclude(event__disruption_type=keep_type)
    links = list(links)
    if not links:
        return
    event_ids = sorted({link.event_id for link in links})
    DisruptionEventSlot.objects.filter(pk__in=[link.pk for link in links]).update(released_at=now)
    for event in DisruptionEvent.objects.filter(pk__in=event_ids).select_for_update():
        if event.scope == DisruptionScope.EQUIPMENT:
            continue
        was_open = event_is_open(event, now)
        _refresh_event_span(event)
        if resume:
            result.resumed.append(event.pk)
            if data.action_taken and data.action_taken != event.action_taken:
                _log_edit(event, "action", data.user, field_name="action_taken", old=event.action_taken,
                          new=data.action_taken)
                event.action_taken = data.action_taken
                event.action_updated_at = now
                event.action_updated_by = _user_or_none(data.user)
        still_to_come = event.slot_links.filter(released_at__isnull=True, start_datetime__gt=now).exists()
        if event.ended_at is None and not still_to_come and (resume or not event.slot_links.filter(
            released_at__isnull=True
        ).exists()):
            event.ended_at = now
            event.ended_by = _user_or_none(data.user)
            event.end_source = data.source
            if was_open:
                result.closed.append(event.pk)
            _log_edit(event, "resumed" if resume else "released", data.user,
                      note="Slots made available" if resume else "Slots changed to another status")
        event.save()


def _find_extendable(equipment, disruption_type: str, run: list[dict], reason: str, now):
    from .models import DisruptionEvent, DisruptionScope

    candidates = (
        DisruptionEvent.objects.filter(
            equipment=equipment,
            disruption_type=disruption_type,
            scope=DisruptionScope.SLOTS,
        )
        .filter(active_events_q(now))
        .order_by("-start_at")[:5]
    )
    for event in candidates:
        if reason and event.reason and reason.strip() != event.reason.strip():
            continue
        linked = list(
            event.slot_links.filter(released_at__isnull=True, daily_slot__isnull=False).values(
                "daily_slot_id", "start_datetime", "end_datetime"
            )
        )
        if not linked:
            continue
        combined = {r["id"]: r for r in run}
        for row in linked:
            combined.setdefault(
                row["daily_slot_id"],
                {"id": row["daily_slot_id"], "start": row["start_datetime"], "end": row["end_datetime"]},
            )
        if len(_group_runs(equipment, list(combined.values()), disruption_type)) == 1:
            return event
    return None


def _open_equipment_event(equipment, disruption_type: str, now):
    from .models import DisruptionEvent, DisruptionScope

    return (
        DisruptionEvent.objects.filter(
            equipment=equipment, disruption_type=disruption_type, scope=DisruptionScope.EQUIPMENT
        )
        .filter(active_events_q(now))
        .order_by("-start_at")
        .first()
    )


def _attach_run(equipment, disruption_type: str, run: list[dict], data: DisruptionInput, bookings: set[int],
                now, result: RecordResult, *, allow_equipment_event: bool = True):
    from .models import DisruptionEvent, DisruptionEventSlot, DisruptionScope

    reason = data.reason
    category = clean_reason_category(disruption_type, data.reason_category)
    event = _open_equipment_event(equipment, disruption_type, now) if allow_equipment_event else None
    created = False
    if event is None:
        event = _find_extendable(equipment, disruption_type, run, reason, now)
    if event is None:
        event = DisruptionEvent.objects.create(
            equipment=equipment,
            disruption_type=disruption_type,
            scope=DisruptionScope.SLOTS,
            source=data.source,
            start_at=min(r["start"] for r in run),
            end_at=max(r["end"] for r in run),
            started_at=now,
            started_by=_user_or_none(data.user),
            reason=reason,
            reason_category=category,
            reason_updated_at=now if (reason or category) else None,
            reason_updated_by=_user_or_none(data.user) if (reason or category) else None,
        )
        created = True
    existing = set(
        DisruptionEventSlot.objects.filter(event=event, daily_slot_id__in=[r["id"] for r in run]).values_list(
            "daily_slot_id", flat=True
        )
    )
    new_links = [
        DisruptionEventSlot(event=event, daily_slot_id=r["id"], start_datetime=r["start"], end_datetime=r["end"])
        for r in run
        if r["id"] not in existing
    ]
    if new_links:
        DisruptionEventSlot.objects.bulk_create(new_links)
    if existing:
        DisruptionEventSlot.objects.filter(event=event, daily_slot_id__in=existing).update(released_at=None)
    if event.scope == DisruptionScope.SLOTS:
        _refresh_event_span(event)
    event.bookings_affected = (event.bookings_affected or 0) + len(bookings)
    if not created and (reason or category) and not (event.reason or event.reason_category):
        event.reason = reason
        event.reason_category = category
        event.reason_updated_at = now
        event.reason_updated_by = _user_or_none(data.user)
    event.save()
    if created:
        _log_edit(event, "created", data.user, note=f"{len(run)} slot(s) marked")
        result.opened.append(event.pk)
    elif new_links:
        _log_edit(event, "extended", data.user, note=f"{len(new_links)} more slot(s) added")
        result.extended.append(event.pk)
    return event


def record_slot_status_change(
    equipment,
    slots,
    new_status: str,
    data: DisruptionInput,
    *,
    affected_booking_ids=None,
    log_change: bool = True,
) -> RecordResult:
    """Call after ``slots`` were changed to ``new_status``. Each slot may carry ``_disruption_old_status``."""
    result = RecordResult()
    try:
        with transaction.atomic():
            _record_slot_status_change(
                equipment, slots, new_status, data, result,
                affected_booking_ids=set(affected_booking_ids or []), log_change=log_change,
            )
    except Exception:
        logger.exception("Could not record disruption for equipment %s", getattr(equipment, "pk", None))
    return result


def _record_slot_status_change(equipment, slots, new_status, data, result, *, affected_booking_ids, log_change):
    from .models import SlotStatusChangeLog

    SlotStatus = _slot_status()
    now = timezone.now()
    rows = _slot_rows(slots)
    if not rows:
        return
    new_status = (new_status or "").strip().upper()
    dtype = disruption_type_for_slot_status(new_status)
    slot_ids = [r["id"] for r in rows]
    _release_links(
        slot_ids,
        keep_type=dtype,
        data=data,
        now=now,
        result=result,
        resume=new_status == SlotStatus.AVAILABLE,
    )
    if dtype:
        if dtype == "OTHER" and not data.reason and data.label:
            data.reason = clean_text(data.label)
        for run in _group_runs(equipment, rows, dtype):
            bookings = {r["booking_id"] for r in run if r["booking_id"] and r["booking_id"] in affected_booking_ids}
            _attach_run(equipment, dtype, run, data, bookings, now, result)
    if log_change:
        previous: dict[str, int] = {}
        for r in rows:
            key = str(r["old_status"] or "")
            previous[key] = previous.get(key, 0) + 1
        log = SlotStatusChangeLog.objects.create(
            equipment=equipment,
            new_status=new_status,
            previous_statuses=previous,
            slot_ids=slot_ids[:2000],
            slot_count=len(slot_ids),
            first_start=min(r["start"] for r in rows),
            last_end=max(r["end"] for r in rows),
            label=clean_text(data.label, 255),
            external_reference=data.external_reference if new_status == SlotStatus.RESERVED_EXTERNAL else "",
            source=data.source,
            bookings_affected=len(affected_booking_ids),
            changed_by=_user_or_none(data.user),
            changed_at=now,
        )
        result.log_id = log.pk


def record_booking_disruption(booking, disruption_type: str, data: DisruptionInput) -> RecordResult:
    """Booking details actions (Under maintenance / Operator unavailable / Analysis not possible) on one booking."""
    result = RecordResult()
    try:
        with transaction.atomic():
            slots = list(booking.daily_slots.all())
            rows = _slot_rows(slots)
            if not rows:
                return result
            now = timezone.now()
            for run in _group_runs(booking.equipment, rows, disruption_type):
                _attach_run(booking.equipment, disruption_type, run, data, {booking.pk}, now, result)
    except Exception:
        logger.exception("Could not record booking disruption for booking %s", getattr(booking, "pk", None))
    return result


def preview_slot_status_change(equipment, slots, new_status: str, *, affected_booking_ids=None, skipped: int = 0):
    """Summary for the confirmation dialog; nothing is changed."""
    from .models import DisruptionEvent, DisruptionEventSlot

    SlotStatus = _slot_status()
    now = timezone.now()
    new_status = (new_status or "").strip().upper()
    dtype = disruption_type_for_slot_status(new_status)
    rows = _slot_rows(slots)
    ids = [r["id"] for r in rows]
    open_event_ids = set(
        DisruptionEventSlot.objects.filter(daily_slot_id__in=ids, released_at__isnull=True)
        .exclude(event__disruption_type=dtype or "")
        .values_list("event_id", flat=True)
    )
    open_events = [
        serialize_event_brief(e)
        for e in DisruptionEvent.objects.filter(pk__in=open_event_ids).filter(active_events_q(now)).select_related(
            "equipment"
        )
    ]
    if new_status == SlotStatus.AVAILABLE:
        equipment_event = _open_equipment_event(equipment, "UNDER_MAINTENANCE", now)
        if equipment_event is not None and any(r["old_status"] == SlotStatus.UNDER_MAINTENANCE for r in rows):
            open_events.append(serialize_event_brief(equipment_event))
    return {
        "slot_count": len(rows),
        "first_start": min((r["start"] for r in rows), default=None),
        "last_end": max((r["end"] for r in rows), default=None),
        "dates": sorted({timezone.localtime(r["start"]).date().isoformat() for r in rows}),
        "bookings_affected": len(set(affected_booking_ids or [])),
        "is_disruption": bool(dtype),
        "disruption_type": dtype,
        "reason_categories": [{"value": k, "label": v} for k, v in REASON_CATEGORIES.get(dtype or "", [])],
        "open_events": open_events,
        "resumes": new_status == SlotStatus.AVAILABLE and bool(open_events),
        "skipped_booked": skipped,
    }


def serialize_event_brief(event) -> dict:
    return {
        "id": event.pk,
        "disruption_type": event.disruption_type,
        "scope": event.scope,
        "start_at": event.start_at,
        "end_at": event.end_at,
        "reason": event.reason,
        "reason_category": event.reason_category,
        "action_taken": event.action_taken,
        "equipment_id": event.equipment_id,
    }


# ---------------------------------------------------------------------------
# Whole equipment
# ---------------------------------------------------------------------------


def record_equipment_under_maintenance(equipment, data: DisruptionInput, *, bookings_affected: int = 0):
    from .models import DisruptionEvent, DisruptionScope, DisruptionType

    result = RecordResult()
    try:
        with transaction.atomic():
            now = timezone.now()
            if _open_equipment_event(equipment, DisruptionType.UNDER_MAINTENANCE, now) is not None:
                return result
            category = clean_reason_category(DisruptionType.UNDER_MAINTENANCE, data.reason_category)
            event = DisruptionEvent.objects.create(
                equipment=equipment,
                disruption_type=DisruptionType.UNDER_MAINTENANCE,
                scope=DisruptionScope.EQUIPMENT,
                source=data.source,
                start_at=now,
                started_at=now,
                started_by=_user_or_none(data.user),
                reason=data.reason,
                reason_category=category,
                reason_updated_at=now if (data.reason or category) else None,
                reason_updated_by=_user_or_none(data.user) if (data.reason or category) else None,
                bookings_affected=max(0, int(bookings_affected or 0)),
            )
            _log_edit(event, "created", data.user, note="Equipment marked Under Maintenance")
            result.opened.append(event.pk)
    except Exception:
        logger.exception("Could not record equipment maintenance for %s", getattr(equipment, "pk", None))
    return result


def record_equipment_operational(equipment, data: DisruptionInput):
    from .models import DisruptionEvent, DisruptionScope

    result = RecordResult()
    try:
        with transaction.atomic():
            now = timezone.now()
            events = DisruptionEvent.objects.select_for_update().filter(
                equipment=equipment, scope=DisruptionScope.EQUIPMENT, ended_at__isnull=True
            )
            for event in events:
                event.ended_at = now
                event.end_at = now
                event.ended_by = _user_or_none(data.user)
                event.end_source = data.source
                if data.action_taken and data.action_taken != event.action_taken:
                    _log_edit(event, "action", data.user, field_name="action_taken", old=event.action_taken,
                              new=data.action_taken)
                    event.action_taken = data.action_taken
                    event.action_updated_at = now
                    event.action_updated_by = _user_or_none(data.user)
                event.save()
                _log_edit(event, "resumed", data.user, note="Equipment marked Operational")
                result.closed.append(event.pk)
                result.resumed.append(event.pk)
    except Exception:
        logger.exception("Could not record equipment operational for %s", getattr(equipment, "pk", None))
    return result


def open_equipment_events_for(equipment) -> list:
    from .models import DisruptionEvent, DisruptionScope

    return list(
        DisruptionEvent.objects.filter(equipment=equipment, scope=DisruptionScope.EQUIPMENT, ended_at__isnull=True)
    )


# ---------------------------------------------------------------------------
# Calendar annotations (staff only)
# ---------------------------------------------------------------------------


def disruption_info_by_slot(equipment, slot_ids) -> dict[int, dict]:
    """slot id -> {event_id, type, reason, reason_category} for unreleased links (one query)."""
    from .models import DisruptionEventSlot

    out: dict[int, dict] = {}
    if not slot_ids:
        return out
    rows = (
        DisruptionEventSlot.objects.filter(daily_slot_id__in=list(slot_ids), released_at__isnull=True)
        .order_by("daily_slot_id", "-event__start_at")
        .values(
            "daily_slot_id",
            "event_id",
            "event__disruption_type",
            "event__reason",
            "event__reason_category",
        )
    )
    for row in rows:
        out.setdefault(
            row["daily_slot_id"],
            {
                "disruption_event_id": row["event_id"],
                "disruption_type": row["event__disruption_type"],
                "disruption_reason": row["event__reason"] or "",
                "disruption_reason_category": reason_category_label(
                    row["event__disruption_type"], row["event__reason_category"]
                ),
            },
        )
    return out


def annotate_slot_payloads_for_staff(equipment, slots, payloads) -> None:
    """Add disruption reason and I-STEM FBR reference to staff slot rows."""
    try:
        SlotStatus = _slot_status()
        info = disruption_info_by_slot(equipment, [getattr(s, "pk", None) for s in slots if getattr(s, "pk", None)])
        equipment_events = None
        refs = {s.pk: (getattr(s, "external_reference", None) or "") for s in slots}
        for row in payloads:
            sid = row.get("id")
            extra = info.get(sid)
            if extra is None and row.get("status") == SlotStatus.UNDER_MAINTENANCE:
                if equipment_events is None:
                    equipment_events = open_equipment_events_for(equipment)
                if equipment_events:
                    ev = equipment_events[0]
                    extra = {
                        "disruption_event_id": ev.pk,
                        "disruption_type": ev.disruption_type,
                        "disruption_reason": ev.reason or "",
                        "disruption_reason_category": reason_category_label(ev.disruption_type, ev.reason_category),
                    }
            if extra:
                row.update(extra)
            if refs.get(sid):
                row["external_reference"] = refs[sid]
    except Exception:
        logger.exception("Could not annotate slot disruption info")
