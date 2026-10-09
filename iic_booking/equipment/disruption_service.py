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


def parse_expected_recovery(raw):
    """``(ok, value)``: empty / "unknown" -> (True, None); an ISO date-time -> (True, aware datetime)."""
    from django.utils.dateparse import parse_datetime

    text = str(raw or "").strip()
    if not text or text.lower() == "unknown":
        return True, None
    value = parse_datetime(text)
    if value is None:
        return False, None
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return True, value


@dataclass
class DisruptionInput:
    user: object | None = None
    source: str = "OTHER"
    reason: str = ""
    reason_category: str = ""
    action_taken: str = ""
    label: str = ""
    external_reference: str = ""
    expected_recovery_at: object | None = None
    expected_recovery_given: bool = False

    @classmethod
    def from_request_data(cls, data, *, user, source: str) -> "DisruptionInput":
        data = data or {}
        given = "expected_recovery_at" in data
        ok, recovery = parse_expected_recovery(data.get("expected_recovery_at")) if given else (True, None)
        return cls(
            user=user,
            source=source,
            reason=clean_text(data.get("disruption_reason")),
            reason_category=str(data.get("disruption_reason_category") or "").strip().upper(),
            action_taken=clean_text(data.get("resolution_action")),
            external_reference=clean_text(data.get("external_reference"), EXTERNAL_REFERENCE_MAX_LENGTH),
            expected_recovery_at=recovery,
            expected_recovery_given=given and ok,
        )


# ---------------------------------------------------------------------------
# Who started / resumed it (role at the time)
# ---------------------------------------------------------------------------

STAFF_ROLE_LABELS = {
    "MAIN_ADMIN": "Main Admin",
    "OIC": "OIC",
    "TEMP_OIC": "Temp OIC",
    "DEPT_ADMIN": "Dept Admin",
    "OPERATOR": "Operator",
    "STAFF": "Staff",
}


def staff_role_label(code: str | None) -> str:
    return STAFF_ROLE_LABELS.get(code or "", "")


def staff_role_for(user, equipment) -> str:
    """Role ``user`` acts in on ``equipment`` right now (stored on the event so later role changes don't alter it)."""
    from iic_booking.users.models.user_type import UserType

    from .models import EquipmentManager, EquipmentOperator, EquipmentTemporaryOIC

    if user is None or not getattr(user, "pk", None):
        return ""
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return "MAIN_ADMIN"
    eid = getattr(equipment, "pk", equipment)
    if eid and EquipmentManager.objects.filter(equipment_id=eid, manager_id=user.pk).exists():
        return "OIC"
    if eid and EquipmentTemporaryOIC.objects.active().filter(equipment_id=eid, temporary_oic_id=user.pk).exists():
        return "TEMP_OIC"
    if ut == UserType.DEPT_ADMIN:
        return "DEPT_ADMIN"
    if ut == UserType.OPERATOR or (
        eid and EquipmentOperator.objects.filter(equipment_id=eid, operator_id=user.pk).exists()
    ):
        return "OPERATOR"
    if ut == UserType.MANAGER:
        return "OIC"
    return "STAFF"


# ---------------------------------------------------------------------------
# Public wording (all users): type, reason, expected recovery. No staff names, actions or reports.
# ---------------------------------------------------------------------------

DISRUPTION_TYPE_LABELS = {
    "UNDER_MAINTENANCE": "Under maintenance",
    "OPERATOR_ABSENT": "Operator absent",
    "SCHEDULED_MAINTENANCE": "Scheduled maintenance",
    "OTHER": "Not available (other reasons)",
}
DEFAULT_PUBLIC_REASONS = {
    "UNDER_MAINTENANCE": "The equipment is under maintenance.",
    "OPERATOR_ABSENT": "The operator is not available at this time.",
    "SCHEDULED_MAINTENANCE": "Planned maintenance of the equipment.",
    "OTHER": "The equipment is not available at this time.",
}
RECOVERY_UNKNOWN_TEXT = "Recovery date not yet announced"
RECOVERY_DELAYED_TEXT = "Recovery delayed — update awaited"


def format_recovery_time(value) -> str:
    local = timezone.localtime(value)
    return f"{local:%a} {local.day} {local:%b}, {local:%H:%M}"


def recovery_status(expected_at, now=None) -> str:
    now = now or timezone.now()
    if expected_at is None:
        return "UNKNOWN"
    return "DELAYED" if expected_at <= now else "EXPECTED"


def recovery_text(expected_at, now=None) -> str:
    state = recovery_status(expected_at, now)
    if state == "UNKNOWN":
        return RECOVERY_UNKNOWN_TEXT
    if state == "DELAYED":
        return RECOVERY_DELAYED_TEXT
    return f"Expected back: {format_recovery_time(expected_at)}"


def public_reason_text(disruption_type: str | None, reason: str = "", category: str = "") -> str:
    text = (reason or "").strip()
    if text:
        return text
    label = reason_category_label(disruption_type, category)
    if label and category != "OTHER":
        return label
    return DEFAULT_PUBLIC_REASONS.get(disruption_type or "", DEFAULT_PUBLIC_REASONS["OTHER"])


def public_disruption_info(disruption_type: str, *, reason: str = "", category: str = "", expected_at=None,
                           ongoing: bool = True, now=None) -> dict:
    out = {
        "type": disruption_type,
        "label": DISRUPTION_TYPE_LABELS.get(disruption_type, "Not available"),
        "reason": public_reason_text(disruption_type, reason, category),
        "expected_recovery_at": expected_at,
        "recovery_status": "",
        "recovery_text": "",
    }
    if ongoing:
        out["recovery_status"] = recovery_status(expected_at, now)
        out["recovery_text"] = recovery_text(expected_at, now)
    return out


def _non_operational_statuses() -> set[str]:
    from .models import EquipmentStatus

    return {EquipmentStatus.REPAIR, EquipmentStatus.MAINTENANCE, EquipmentStatus.INACTIVE}


def public_equipment_notice(equipment, now=None) -> dict | None:
    """Status notice for the equipment page and card while the equipment is not operational; None otherwise."""
    from .models import DisruptionEvent, DisruptionScope

    if getattr(equipment, "status", None) not in _non_operational_statuses():
        return None
    now = now or timezone.now()
    event = (
        DisruptionEvent.objects.filter(
            equipment_id=equipment.pk, scope=DisruptionScope.EQUIPMENT, ended_at__isnull=True, is_deleted=False
        )
        .order_by("-start_at")
        .only("disruption_type", "reason", "reason_category", "expected_recovery_at", "start_at")
        .first()
    )
    info = public_disruption_info(
        "UNDER_MAINTENANCE",
        reason=event.reason if event else "",
        category=event.reason_category if event else "",
        expected_at=event.expected_recovery_at if event else None,
        now=now,
    )
    info["since"] = event.start_at if event else None
    info["message"] = f"Under maintenance · {info['recovery_text']}"
    return info


def set_expected_recovery(event, value, user, now=None) -> bool:
    """Change the expected recovery (logged). Returns True when it changed."""
    if event.expected_recovery_at == value:
        return False
    _log_edit(
        event,
        "recovery",
        user,
        field_name="expected_recovery_at",
        old=event.expected_recovery_at.isoformat() if event.expected_recovery_at else "",
        new=value.isoformat() if value else "",
        note=f"Expected back: {format_recovery_time(value)}" if value else RECOVERY_UNKNOWN_TEXT,
    )
    event.expected_recovery_at = value
    return True


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
        # Deleted events are still kept consistent (in case they are restored) but never reported back.
        visible = not event.is_deleted
        was_open = event_is_open(event, now)
        _refresh_event_span(event)
        if resume and visible:
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
            event.ended_by_role = staff_role_for(event.ended_by, event.equipment_id)
            event.end_source = data.source
            if was_open and visible:
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
            is_deleted=False,
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
            equipment=equipment, disruption_type=disruption_type, scope=DisruptionScope.EQUIPMENT, is_deleted=False
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
            started_by_role=staff_role_for(_user_or_none(data.user), equipment),
            reason=reason,
            reason_category=category,
            reason_updated_at=now if (reason or category) else None,
            reason_updated_by=_user_or_none(data.user) if (reason or category) else None,
            expected_recovery_at=data.expected_recovery_at if data.expected_recovery_given else None,
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
    if not created and data.expected_recovery_given:
        set_expected_recovery(event, data.expected_recovery_at, data.user, now)
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
        for e in DisruptionEvent.objects.filter(pk__in=open_event_ids, is_deleted=False)
        .filter(active_events_q(now))
        .select_related(
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
            existing = _open_equipment_event(equipment, DisruptionType.UNDER_MAINTENANCE, now)
            if existing is not None:
                if data.expected_recovery_given and set_expected_recovery(
                    existing, data.expected_recovery_at, data.user, now
                ):
                    existing.save(update_fields=["expected_recovery_at", "updated_at"])
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
                started_by_role=staff_role_for(_user_or_none(data.user), equipment),
                expected_recovery_at=data.expected_recovery_at if data.expected_recovery_given else None,
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
                event.ended_by_role = staff_role_for(event.ended_by, equipment)
                event.end_source = data.source
                if data.action_taken and data.action_taken != event.action_taken:
                    _log_edit(event, "action", data.user, field_name="action_taken", old=event.action_taken,
                              new=data.action_taken)
                    event.action_taken = data.action_taken
                    event.action_updated_at = now
                    event.action_updated_by = _user_or_none(data.user)
                event.save()
                _log_edit(event, "resumed", data.user, note="Equipment marked Operational")
                if not event.is_deleted:
                    result.closed.append(event.pk)
                    result.resumed.append(event.pk)
    except Exception:
        logger.exception("Could not record equipment operational for %s", getattr(equipment, "pk", None))
    return result


def open_equipment_events_for(equipment) -> list:
    from .models import DisruptionEvent, DisruptionScope

    return list(
        DisruptionEvent.objects.filter(
            equipment=equipment, scope=DisruptionScope.EQUIPMENT, ended_at__isnull=True, is_deleted=False
        )
    )


# ---------------------------------------------------------------------------
# Automatic paths (slot generation, equipment maintenance, freed bookings)
# ---------------------------------------------------------------------------


def auto_recorded_slot_statuses() -> list[str]:
    """Statuses that are always a disruption. BLOCKED is left out: repeat rules, holidays and training block too."""
    SlotStatus = _slot_status()
    return [SlotStatus.UNDER_MAINTENANCE, SlotStatus.OPERATOR_ABSENT, SlotStatus.SCHEDULED_MAINTENANCE]


def unrecorded_disruption_slots(slots_qs):
    """Slots of ``slots_qs`` in an auto-recorded status with no unreleased event link.

    A link to a deleted event still counts as recorded: staff removed that entry on purpose."""
    from .models import DisruptionEventSlot

    return slots_qs.filter(status__in=auto_recorded_slot_statuses()).exclude(
        pk__in=DisruptionEventSlot.objects.filter(released_at__isnull=True, daily_slot__isnull=False).values(
            "daily_slot_id"
        )
    )


def record_unrecorded_slots(equipment, slots_qs, data: DisruptionInput, *, old_status: str | None = None) -> RecordResult:
    """Record slots that a path other than a staff status change put into a disruption status.

    Under Maintenance slots join the open whole-equipment event when there is one; other runs open or extend slot
    events exactly like a staff change. Already recorded slots are left alone, so calling this twice is harmless."""
    result = RecordResult()
    try:
        slots = list(
            unrecorded_disruption_slots(slots_qs)
            .only("id", "start_datetime", "end_datetime", "status", "booking_id")
            .order_by("start_datetime", "id")
        )
    except Exception:
        logger.exception("Could not find unrecorded disruption slots for equipment %s", getattr(equipment, "pk", None))
        return result
    by_status: dict[str, list] = {}
    for slot in slots:
        if old_status:
            slot._disruption_old_status = old_status
        by_status.setdefault(slot.status, []).append(slot)
    for status, group in by_status.items():
        part = record_slot_status_change(equipment, group, status, data)
        result.opened += part.opened
        result.extended += part.extended
    return result


def release_slots_made_available(equipment, slot_ids, data: DisruptionInput) -> RecordResult:
    """Release the event links of slots an automatic path put back to Available (e.g. equipment Operational)."""
    from .models import DailySlot

    SlotStatus = _slot_status()
    if not slot_ids:
        return RecordResult()
    slots = list(
        DailySlot.objects.filter(pk__in=list(slot_ids), status=SlotStatus.AVAILABLE).only(
            "id", "start_datetime", "end_datetime", "status", "booking_id"
        )
    )
    for slot in slots:
        slot._disruption_old_status = SlotStatus.UNDER_MAINTENANCE
    return record_slot_status_change(equipment, slots, SlotStatus.AVAILABLE, data)


def record_unrecorded_upcoming_slots(today=None) -> dict[str, int]:
    """Safety net for the daily sweep: record today's and future disruption slots that no path recorded."""
    from .models import DailySlot, Equipment

    today = today or timezone.localdate()
    base = DailySlot.objects.filter(date__gte=today, slot_master__equipment__isnull=False)
    equipment_ids = sorted(
        set(unrecorded_disruption_slots(base).values_list("slot_master__equipment_id", flat=True))
    )
    stats = {"equipment": 0, "opened": 0, "extended": 0}
    for equipment in Equipment.objects.filter(pk__in=equipment_ids):
        result = record_unrecorded_slots(
            equipment, base.filter(slot_master__equipment=equipment), DisruptionInput(source="OTHER")
        )
        stats["equipment"] += 1
        stats["opened"] += len(result.opened)
        stats["extended"] += len(result.extended)
    return stats


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
        DisruptionEventSlot.objects.filter(
            daily_slot_id__in=list(slot_ids), released_at__isnull=True, event__is_deleted=False
        )
        .order_by("daily_slot_id", "-event__start_at")
        .values(
            "daily_slot_id",
            "event_id",
            "event__disruption_type",
            "event__reason",
            "event__reason_category",
            "event__expected_recovery_at",
            "event__ended_at",
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
                "_category_key": row["event__reason_category"] or "",
                "_expected_recovery_at": row["event__expected_recovery_at"],
                "_ended": row["event__ended_at"] is not None,
            },
        )
    return out


def _public_info_for_row(dtype, ev, slot_end, now) -> dict:
    ongoing = slot_end is None or slot_end > now
    if ev is None:
        return public_disruption_info(dtype, ongoing=ongoing, now=now)
    return public_disruption_info(
        ev["disruption_type"],
        reason=ev["disruption_reason"],
        category=ev["_category_key"],
        expected_at=ev["_expected_recovery_at"],
        ongoing=ongoing and not ev["_ended"],
        now=now,
    )


def _equipment_event_info(ev) -> dict:
    return {
        "disruption_event_id": ev.pk,
        "disruption_type": ev.disruption_type,
        "disruption_reason": ev.reason or "",
        "disruption_reason_category": reason_category_label(ev.disruption_type, ev.reason_category),
        "_category_key": ev.reason_category or "",
        "_expected_recovery_at": ev.expected_recovery_at,
        "_ended": ev.ended_at is not None,
    }


def annotate_slot_payloads_public(equipment, slots, payloads) -> None:
    """Every user: ``disruption_public`` (type, reason or default wording, expected recovery) on disrupted slots.

    Blocks without a recorded disruption (holidays, repeat rules) and Not Available are left alone; deleted
    disruptions are ignored. Never includes staff names, actions taken or service reports."""
    try:
        now = timezone.now()
        candidates = [row for row in payloads if disruption_type_for_slot_status(row.get("status"))]
        if not candidates:
            return
        ends = {s.pk: getattr(s, "end_datetime", None) for s in slots if getattr(s, "pk", None)}
        info = disruption_info_by_slot(equipment, [row.get("id") for row in candidates if row.get("id")])
        equipment_events = None
        for row in candidates:
            dtype = disruption_type_for_slot_status(row.get("status"))
            ev = info.get(row.get("id"))
            if ev is None and dtype == "UNDER_MAINTENANCE":
                if equipment_events is None:
                    equipment_events = open_equipment_events_for(equipment)
                if equipment_events:
                    ev = _equipment_event_info(equipment_events[0])
            if ev is None and dtype == "OTHER":
                continue
            row["disruption_public"] = _public_info_for_row(dtype, ev, ends.get(row.get("id")), now)
    except Exception:
        logger.exception("Could not annotate public slot disruption info")


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
                    extra = _equipment_event_info(equipment_events[0])
            if extra:
                row.update({k: v for k, v in extra.items() if not k.startswith("_")})
            if refs.get(sid):
                row["external_reference"] = refs[sid]
    except Exception:
        logger.exception("Could not annotate slot disruption info")
