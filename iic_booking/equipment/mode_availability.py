"""
Read-only availability summary for multi-mode equipment (equipment page section and catalog cards).

Which mode runs on a day comes from the family's mode schedules, with the same rules booking uses: a mode
set to "Only on scheduled days" runs only on days one of its schedules covers, and a mutually exclusive
schedule blocks the base and the other modes. Free-slot counts come from existing DailySlot rows inside
the viewer's open booking window; days after that window only show the recurring pattern and when booking
opens. Never generates slots and never reports slot data outside the window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable, Optional

from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .models import (
    DailySlot,
    Equipment,
    EquipmentModeSchedule,
    EquipmentStatus,
    Holiday,
    ModeAvailability,
    ModeScheduleBehavior,
    SlotStatus,
)

DEFAULT_DAYS = 28
MAX_DAYS = 42
CACHE_SECONDS = 60
_CACHE_PREFIX = "mm_avail:v1"

AVAILABLE = "available"
FULL = "full"
NOT_AVAILABLE = "not_available"
HOLIDAY = "holiday"
CLOSED = "closed"
MAINTENANCE = "maintenance"
NOT_OPEN = "not_open"
NOT_RUNNING = "not_running"
PAST = "past"

VIEWER_ADMIN = "admin"
VIEWER_EXTERNAL = "external"
VIEWER_INTERNAL = "internal"

_BOOKED_STATUSES = {SlotStatus.BOOKED, SlotStatus.RESERVED_EXTERNAL, SlotStatus.BOOKING_NOT_UTILIZED}
_MAINTENANCE_STATUSES = {SlotStatus.UNDER_MAINTENANCE, SlotStatus.SCHEDULED_MAINTENANCE}
_WEEKEND_REASONS = {"Saturday", "Sunday"}

_MEMBER_FIELDS = (
    "equipment_id",
    "code",
    "name",
    "status",
    "parent_equipment_id",
    "enable_multi_mode",
    "mode_availability",
    "internal_department_id",
    "slot_window_reference_weekday",
    "slot_window_reference_time",
    "weekly_view_time_from",
    "weekly_view_time_to",
    "reschedule_hours_threshold",
)


@dataclass(frozen=True)
class Viewer:
    kind: str
    department_id: Optional[int] = None
    signed_in: bool = False

    @property
    def cache_part(self) -> str:
        dept = self.department_id if self.kind == VIEWER_INTERNAL and self.signed_in else "-"
        return f"{self.kind}:{int(self.signed_in)}:{dept}"


def viewer_for(user) -> Viewer:
    if not user or not getattr(user, "is_authenticated", False):
        return Viewer(VIEWER_INTERNAL)
    user_type = getattr(user, "user_type", None)
    if user_type in UserType.get_admin_panel_codes():
        return Viewer(VIEWER_ADMIN, signed_in=True)
    if user_type and UserType.is_external_user(user_type):
        return Viewer(VIEWER_EXTERNAL, signed_in=True)
    return Viewer(VIEWER_INTERNAL, getattr(user, "department_id", None), True)


# --- booking window (mirrors the weekly slots API) ---------------------------------------------------


@dataclass(frozen=True)
class Window:
    min_date: date
    max_date: date
    reference: Optional[tuple[int, time]]


def _global_reference() -> Optional[tuple[int, time]]:
    from .api_views import get_internal_slot_window_setting

    setting = get_internal_slot_window_setting()
    if setting is None:
        return None
    return int(setting.reference_weekday), setting.reference_time


def _reference(equipment, global_reference) -> Optional[tuple[int, time]]:
    weekday = getattr(equipment, "slot_window_reference_weekday", None)
    at = getattr(equipment, "slot_window_reference_time", None)
    if weekday is not None and at is not None:
        return int(weekday), at
    return global_reference


def _reference_instant(monday: date, reference: tuple[int, time]) -> datetime:
    naive = datetime.combine(monday + timedelta(days=reference[0]), reference[1])
    return timezone.make_aware(naive, timezone.get_current_timezone())


def booking_window(equipment, viewer: Viewer, global_reference, now: datetime) -> Window:
    """Dates the viewer can currently see slots for: internal users this week (plus next week once the weekly
    reference instant passes); external users one week later; staff this and next week."""
    local = timezone.localtime(now)
    today = local.date()
    monday = today - timedelta(days=today.weekday())
    if viewer.kind == VIEWER_ADMIN:
        return Window(today, monday + timedelta(days=13), None)
    reference = _reference(equipment, global_reference)
    before = reference is not None and local < _reference_instant(monday, reference)
    if viewer.kind == VIEWER_EXTERNAL:
        last = 13 if reference is None or before else 20
        return Window(monday + timedelta(days=7), monday + timedelta(days=last), reference)
    if reference is None:
        return Window(today, monday + timedelta(days=13), None)
    return Window(today, monday + timedelta(days=6 if before else 13), reference)


def opens_at(window: Window, viewer: Viewer, on_date: date) -> datetime:
    """When ``on_date`` (after the window) becomes visible for booking to this kind of viewer."""
    week = on_date - timedelta(days=on_date.weekday())
    if window.reference is None:
        start = datetime.combine(week - timedelta(days=7), time.min)
        return timezone.make_aware(start, timezone.get_current_timezone())
    lead = 14 if viewer.kind == VIEWER_EXTERNAL else 7
    return _reference_instant(week - timedelta(days=lead), window.reference)


# --- mode rules ----------------------------------------------------------------------------------------


def _first_active(schedules, on_date: date, at_time: Optional[time], *, mode_id=None, exclude_mode_id=None,
                  behavior=None) -> Optional[EquipmentModeSchedule]:
    from .mode_utils import _first_active as first_active

    return first_active(
        schedules, on_date, at_time, mode_id=mode_id, exclude_mode_id=exclude_mode_id, behavior=behavior
    )


def mode_day_rule(mode: Equipment, schedules: list[EquipmentModeSchedule], on_date: date) -> dict:
    """Whether ``mode`` runs on ``on_date``: {runs, partial, label, blocked_by}."""
    covering = [s for s in schedules if s.covers_date(on_date)]
    partial = False
    if mode.parent_equipment_id and mode.mode_availability == ModeAvailability.SCHEDULED_ONLY:
        own = [s for s in covering if s.mode_equipment_id == mode.equipment_id]
        if not own:
            own_all = [s for s in schedules if s.mode_equipment_id == mode.equipment_id]
            label = (own_all[-1].unavailable_label if own_all else "") or "Mode not scheduled"
            return {"runs": False, "partial": False, "label": label, "blocked_by": None}
        partial = all(s.has_time_window() for s in own)
    exclusive = [
        s for s in covering
        if s.behavior == ModeScheduleBehavior.EXCLUSIVE and s.mode_equipment_id != mode.equipment_id
    ]
    whole_day = [s for s in exclusive if not s.has_time_window()]
    if whole_day:
        s = whole_day[0]
        return {
            "runs": False,
            "partial": False,
            "label": s.exclusive_blocked_label or "Alternate mode active",
            "blocked_by": s.mode_equipment_id,
        }
    return {"runs": True, "partial": partial or bool(exclusive), "label": "", "blocked_by": None}


def _slot_allowed_by_mode(mode: Equipment, schedules, on_date: date, at_time: time) -> bool:
    """Same rule as the slot overlay users see on the booking page."""
    exclusive = ModeScheduleBehavior.EXCLUSIVE
    if mode.parent_equipment_id:
        if (
            mode.mode_availability == ModeAvailability.SCHEDULED_ONLY
            and _first_active(schedules, on_date, at_time, mode_id=mode.equipment_id) is None
        ):
            return False
        return _first_active(schedules, on_date, at_time, exclude_mode_id=mode.equipment_id, behavior=exclusive) is None
    return _first_active(schedules, on_date, at_time, behavior=exclusive) is None


def _mode_hours(mode: Equipment, schedules, start: date, end: date) -> list[str]:
    if not mode.parent_equipment_id:
        return []
    out = []
    for s in schedules:
        if s.mode_equipment_id != mode.equipment_id or not s.has_time_window():
            continue
        if s.start_date and s.start_date > end or s.end_date and s.end_date < start:
            continue
        text = f"{s.start_time.strftime('%H:%M')}–{s.end_time.strftime('%H:%M')}"
        if text not in out:
            out.append(text)
    return out


# --- data loading (batched) ----------------------------------------------------------------------------


def _load_families(parent_ids: Iterable[int]) -> dict[int, list[Equipment]]:
    parent_ids = {int(p) for p in parent_ids}
    if not parent_ids:
        return {}
    rows = list(
        Equipment.objects.filter(Q(pk__in=parent_ids) | Q(parent_equipment_id__in=parent_ids)).only(*_MEMBER_FIELDS)
    )
    families = {}
    for parent in rows:
        if parent.pk not in parent_ids or parent.parent_equipment_id or not parent.enable_multi_mode:
            continue
        children = sorted(
            (e for e in rows if e.parent_equipment_id == parent.pk), key=lambda c: (c.code or "", c.name or "")
        )
        if children:
            families[parent.pk] = [parent, *children]
    return families


def _load_schedules(parent_ids: Iterable[int]) -> dict[int, list[EquipmentModeSchedule]]:
    out: dict[int, list[EquipmentModeSchedule]] = {}
    for s in EquipmentModeSchedule.objects.filter(parent_equipment_id__in=list(parent_ids)).order_by("id"):
        out.setdefault(s.parent_equipment_id, []).append(s)
    return out


def _load_slot_rows(equipment_ids: list[int], date_from: date, date_to: date) -> dict[tuple[int, date], list[tuple]]:
    out: dict[tuple[int, date], list[tuple]] = {}
    if not equipment_ids or date_from > date_to:
        return out
    rows = DailySlot.objects.filter(
        slot_master__equipment_id__in=equipment_ids, date__gte=date_from, date__lte=date_to
    ).values_list(
        "slot_master__equipment_id", "date", "status", "start_datetime", "end_datetime", "booking_id",
        "home_department_only",
    )
    for row in rows:
        out.setdefault((row[0], row[1]), []).append(row[2:])
    return out


# --- per-family summary ----------------------------------------------------------------------------------


def _local_time(dt: Optional[datetime]) -> Optional[time]:
    if dt is None:
        return None
    return (timezone.localtime(dt) if timezone.is_aware(dt) else dt).time()


def _within_view_window(mode: Equipment, start_dt, end_dt) -> bool:
    time_from, time_to = mode.weekly_view_time_from, mode.weekly_view_time_to
    st, et = _local_time(start_dt), _local_time(end_dt)
    if st is None or et is None:
        return False
    if time_from is not None and st < time_from:
        return False
    if time_to is not None and et > time_to:
        return False
    return True


def _department_rule(mode: Equipment, viewer: Viewer, policy_active: bool, now: datetime):
    """None when every slot is open to the viewer; else a function(row) -> allowed (home-department split)."""
    if viewer.kind != VIEWER_INTERNAL or not viewer.signed_in or not mode.internal_department_id or not policy_active:
        return None
    is_home = viewer.department_id == mode.internal_department_id
    boundary = now + timedelta(hours=int(mode.reschedule_hours_threshold or 48))

    def allowed(row) -> bool:
        marked = bool(row[4])
        if is_home:
            return not marked or (row[1] is not None and row[1] <= boundary)
        return marked

    return allowed


def _fmt_day(d: date) -> str:
    return d.strftime("%a %d %b").replace(" 0", " ")


def _day_status(mode, schedules, viewer, window, rule, rows, on_date, holiday, now, dept_allowed) -> dict:
    is_weekend = holiday is not None and holiday in _WEEKEND_REASONS and on_date.weekday() >= 5
    if not rule["runs"]:
        return {"status": NOT_RUNNING, "label": rule["label"], "blocked_by": rule["blocked_by"]}
    if on_date > window.max_date:
        if holiday is not None and not is_weekend:
            return {"status": HOLIDAY, "label": holiday}
        if is_weekend:
            return {"status": CLOSED, "label": "Weekend"}
        at = opens_at(window, viewer, on_date)
        return {"status": NOT_OPEN, "label": "Booking not open yet", "opens_at": at.isoformat()}
    if on_date < window.min_date:
        return {"status": NOT_AVAILABLE, "label": "Outside your booking window"}
    if mode.status != EquipmentStatus.ACTIVE:
        return {"status": MAINTENANCE, "label": "Under maintenance"}
    if viewer.kind == VIEWER_INTERNAL:
        rows = [r for r in rows if _within_view_window(mode, r[1], r[2])]
    open_rows = [
        r for r in rows
        if r[0] == SlotStatus.AVAILABLE and r[3] is None and r[1] is not None and r[1] > now
        and _slot_allowed_by_mode(mode, schedules, on_date, _local_time(r[1]))
    ]
    free = [r for r in open_rows if dept_allowed is None or dept_allowed(r)]
    out: dict[str, Any] = {"partial": rule["partial"]} if rule["partial"] else {}
    if free:
        first = min(r[1] for r in free)
        n = len(free)
        return {
            **out, "status": AVAILABLE, "label": f"{n} free slot{'s' if n != 1 else ''}", "free_slots": n,
            "total_slots": len(rows), "first_slot_at": first.isoformat(),
        }
    booked = any(r[0] in _BOOKED_STATUSES or r[3] is not None for r in rows)
    opened = booked or any(r[0] == SlotStatus.AVAILABLE for r in rows)
    if not rows or (holiday is not None and not opened):
        if holiday is not None:
            return {**out, "status": CLOSED if is_weekend else HOLIDAY, "label": "Weekend" if is_weekend else holiday}
        return {**out, "status": NOT_AVAILABLE, "label": "No slots"}
    if any(r[0] in _MAINTENANCE_STATUSES for r in rows) and not booked:
        return {**out, "status": MAINTENANCE, "label": "Maintenance"}
    if open_rows:
        return {**out, "status": NOT_AVAILABLE, "label": "Reserved for another department"}
    if booked:
        return {**out, "status": FULL, "label": "Fully booked", "total_slots": len(rows)}
    if on_date == timezone.localtime(now).date() and any(r[0] == SlotStatus.AVAILABLE for r in rows):
        return {**out, "status": NOT_AVAILABLE, "label": "No more slots today"}
    return {**out, "status": NOT_AVAILABLE, "label": "Not available"}


def _summarize_family(members, schedules, viewer, *, start, days, now, global_reference, holidays, slot_rows) -> dict:
    today = timezone.localtime(now).date()
    end = start + timedelta(days=days - 1)
    windows = {m.pk: booking_window(m, viewer, global_reference, now) for m in members}
    modes_out = []
    day_cells: dict[date, list[dict]] = {start + timedelta(days=i): [] for i in range(days)}
    for mode in members:
        window = windows[mode.pk]
        policy_active = any(
            r[4] for (eq_id, d), rows in slot_rows.items() if eq_id == mode.pk and d >= today for r in rows
        )
        dept_allowed = _department_rule(mode, viewer, policy_active, now)
        weekend_open = {5: False, 6: False}
        for (eq_id, d), rows in slot_rows.items():
            if eq_id == mode.pk and d.weekday() >= 5 and any(
                r[0] == SlotStatus.AVAILABLE or r[0] in _BOOKED_STATUSES or r[3] is not None for r in rows
            ):
                weekend_open[d.weekday()] = True
        weekdays: set[int] = set()
        next_available = None
        next_opening = None
        full_days = 0
        for d in day_cells:
            if d < today:
                day_cells[d].append({"equipment_id": mode.pk, "status": PAST, "label": ""})
                continue
            rule = mode_day_rule(mode, schedules, d)
            if rule["runs"] and (d.weekday() < 5 or weekend_open[d.weekday()]):
                weekdays.add(d.weekday())
            cell = _day_status(
                mode, schedules, viewer, window, rule, slot_rows.get((mode.pk, d), []), d, holidays.get(d), now,
                dept_allowed,
            )
            cell["equipment_id"] = mode.pk
            day_cells[d].append(cell)
            if cell["status"] == AVAILABLE and next_available is None:
                next_available = {"date": d.isoformat(), "free_slots": cell["free_slots"],
                                  "first_slot_at": cell["first_slot_at"]}
            elif cell["status"] == NOT_OPEN and next_opening is None:
                next_opening = {"date": d.isoformat(), "opens_at": cell["opens_at"]}
            elif cell["status"] == FULL:
                full_days += 1
        if mode.status != EquipmentStatus.ACTIVE:
            state = MAINTENANCE
        elif next_available:
            state = AVAILABLE
        elif full_days:
            state = FULL
        elif next_opening:
            state = NOT_OPEN
        else:
            state = NOT_RUNNING
        modes_out.append(
            {
                "equipment_id": mode.pk,
                "code": mode.code,
                "name": mode.name,
                "role": "mode" if mode.parent_equipment_id else "base",
                "operational": mode.status == EquipmentStatus.ACTIVE,
                "mode_availability": mode.mode_availability if mode.parent_equipment_id else None,
                "weekdays": sorted(weekdays),
                "hours": _mode_hours(mode, schedules, today, end),
                "window": {"min_date": window.min_date.isoformat(), "max_date": window.max_date.isoformat()},
                "state": state,
                "next_available": next_available,
                "next_opening": next_opening,
            }
        )
    day_rows = []
    for d, cells in day_cells.items():
        reason = holidays.get(d)
        weekend = d.weekday() >= 5 and (reason is None or reason in _WEEKEND_REASONS)
        day_rows.append(
            {
                "date": d.isoformat(),
                "weekday": d.weekday(),
                "is_today": d == today,
                "is_past": d < today,
                "holiday": None if weekend else reason,
                "weekend": weekend,
                "modes": cells,
            }
        )
    return {
        "multi_mode": True,
        "parent_equipment_id": members[0].pk,
        "generated_at": now.isoformat(),
        "today": today.isoformat(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "days": day_rows,
        "modes": modes_out,
    }


def _normalize_days(days) -> int:
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = DEFAULT_DAYS
    n = max(7, min(MAX_DAYS, n))
    return ((n + 6) // 7) * 7


def family_summaries(parent_ids: Iterable[int], viewer: Viewer, *, days: int = DEFAULT_DAYS, now=None) -> dict[int, dict]:
    """Full summaries for the given base ids (bases without modes are left out). Cached briefly per viewer kind."""
    now = now or timezone.now()
    days = _normalize_days(days)
    today = timezone.localtime(now).date()
    start = today - timedelta(days=today.weekday())
    parent_ids = sorted({int(p) for p in parent_ids})
    if not parent_ids:
        return {}
    keys = {pid: f"{_CACHE_PREFIX}:{pid}:{viewer.cache_part}:{start.isoformat()}:{days}" for pid in parent_ids}
    cached = cache.get_many(list(keys.values()))
    out: dict[int, dict] = {}
    missing = []
    for pid, key in keys.items():
        if key in cached:
            if cached[key].get("multi_mode"):
                out[pid] = cached[key]
        else:
            missing.append(pid)
    if not missing:
        return out
    families = _load_families(missing)
    to_cache = {keys[pid]: {"multi_mode": False} for pid in missing if pid not in families}
    if families:
        schedules = _load_schedules(families.keys())
        end = start + timedelta(days=days - 1)
        holidays = {d: v["reason"] for d, v in Holiday.get_holidays_in_range(today, end).items()}
        global_reference = _global_reference()
        windows_end = max(
            booking_window(m, viewer, global_reference, now).max_date for members in families.values() for m in members
        )
        slot_rows = _load_slot_rows(
            [m.pk for members in families.values() for m in members], today, min(end, windows_end)
        )
        for pid, members in families.items():
            summary = _summarize_family(
                members, schedules.get(pid, []), viewer, start=start, days=days, now=now,
                global_reference=global_reference, holidays=holidays, slot_rows=slot_rows,
            )
            out[pid] = summary
            to_cache[keys[pid]] = summary
    cache.set_many(to_cache, CACHE_SECONDS)
    return out


def family_parent_id(equipment) -> int:
    return getattr(equipment, "parent_equipment_id", None) or equipment.pk


def restrict_to_members(summary: dict, member_ids: set[int]) -> dict:
    """Copy of ``summary`` keeping only the given family members."""
    return {
        **summary,
        "modes": [m for m in summary["modes"] if m["equipment_id"] in member_ids],
        "days": [
            {**d, "modes": [c for c in d["modes"] if c["equipment_id"] in member_ids]} for d in summary["days"]
        ],
    }


def visible_member_ids(user, member_ids: Iterable[int]) -> set[int]:
    """Family members the viewer may see (the mode-of-the-day catalog rule is not applied here)."""
    from .api_views import get_visible_equipment_queryset

    scope = "all" if getattr(user, "is_authenticated", False) and getattr(user, "user_type", None) == UserType.MANAGER else None
    qs = get_visible_equipment_queryset(user, catalog_scope=scope, mode_catalog_filter=False)
    return set(qs.filter(pk__in=list(member_ids)).values_list("pk", flat=True))


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_mode_availability(request, pk):
    """Availability summary of the multi-mode family of equipment ``pk`` (same visibility as the equipment page)."""
    from .api_views import (
        equipment_visibility_denied_response,
        user_can_see_equipment,
        user_can_view_equipment_in_catalog,
    )

    equipment = Equipment.objects.filter(pk=pk).first()
    if equipment is None:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    user = request.user
    if not user_can_see_equipment(user, equipment) and not user_can_view_equipment_in_catalog(user, equipment):
        return equipment_visibility_denied_response(user)
    parent_id = family_parent_id(equipment)
    summary = family_summaries(
        [parent_id], viewer_for(user), days=request.query_params.get("days") or DEFAULT_DAYS
    ).get(parent_id)
    if summary is None:
        return Response({"equipment_id": equipment.pk, "multi_mode": False})
    member_ids = {m["equipment_id"] for m in summary["modes"]}
    visible = visible_member_ids(user, member_ids) | {equipment.pk}
    return Response({**restrict_to_members(summary, visible), "equipment_id": equipment.pk})


_CARD_FIELDS = ("equipment_id", "code", "name", "role", "operational", "weekdays", "hours", "state",
                "next_available", "next_opening")


def card_entry(mode: dict) -> dict:
    out = {k: mode.get(k) for k in _CARD_FIELDS}
    if out["next_available"]:
        out["next_available"] = {k: out["next_available"][k] for k in ("date", "free_slots")}
    return out


def attach_card_availability(rows: list[dict], user, visible_child_ids: set[int]) -> None:
    """Add a compact ``mode_availability`` to catalog rows of multi-mode equipment (bases and modes)."""
    family_of: dict[int, int] = {}
    for row in rows:
        parent = row.get("parent_equipment")
        if parent:
            family_of[row["equipment_id"]] = int(parent)
        elif row.get("enable_multi_mode"):
            family_of[row["equipment_id"]] = row["equipment_id"]
    if not family_of:
        return
    summaries = family_summaries(set(family_of.values()), viewer_for(user))
    for row in rows:
        pid = family_of.get(row["equipment_id"])
        summary = summaries.get(pid) if pid is not None else None
        if summary is None:
            continue
        if pid == row["equipment_id"]:
            wanted = {pid} | set(visible_child_ids)
        else:
            wanted = {row["equipment_id"]}
        modes = [card_entry(m) for m in summary["modes"] if m["equipment_id"] in wanted]
        if modes:
            row["mode_availability"] = {"parent_equipment_id": pid, "today": summary["today"], "modes": modes}
