"""Preferred recurring slot on a booking template, and the opt-in "if my slot is taken" fallback.

A template stores a weekday + start time (+ number of consecutive slots), not a date. Loading the template
resolves it to the next occurrence inside the user's currently open booking window; the user still clicks
Book. Nothing is booked server-side on a schedule.

The fallback only runs inside the user's own book request: when the selected slots were taken and the
template has an explicit, consented ``if_slot_taken`` choice, the next free run of the same length and
duration is claimed in the same transaction, after which every normal booking check still applies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import BookingInputTemplate, DailySlot, SlotMaster, SlotStatus

IF_SLOT_TAKEN_ASK = "ask"
IF_SLOT_TAKEN_SAME_DAY = "next_available_same_day"
IF_SLOT_TAKEN_ANY = "next_available_any"
IF_SLOT_TAKEN_CHOICES = (IF_SLOT_TAKEN_ASK, IF_SLOT_TAKEN_SAME_DAY, IF_SLOT_TAKEN_ANY)
AUTO_NEXT_MODES = frozenset({IF_SLOT_TAKEN_SAME_DAY, IF_SLOT_TAKEN_ANY})

MAX_PREFERRED_SLOT_COUNT = 24
ALTERNATIVES_LIMIT = 5
# Bounded scans: the 9 PM rush must not turn a lost race into a long table walk while holding locks.
_CANDIDATE_SCAN_LIMIT = 400
_LOCK_ATTEMPTS = 5

WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


@dataclass(frozen=True)
class BookingWindow:
    min_date: date
    max_date: date
    # Set while next week is still closed: the instant it opens (e.g. Wednesday 21:00).
    opens_at: datetime | None


# --- template field cleaning / serialization ------------------------------------------------


def _parse_start_time(raw) -> time | None:
    if isinstance(raw, time):
        return raw.replace(second=0, microsecond=0)
    text = str(raw or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time().replace(second=0)
        except ValueError:
            continue
    return None


def clean_preferred_slot(raw, equipment):
    """Return ({weekday, start_time, slot_count, slot_master_id} | None, error)."""
    if raw is None or raw == {}:
        return None, None
    if not isinstance(raw, dict):
        return None, "preferred_slot must be an object or null."
    weekday = raw.get("weekday")
    if isinstance(weekday, bool) or not isinstance(weekday, int) or not 0 <= weekday <= 6:
        return None, "Choose the preferred slot's weekday."
    start = _parse_start_time(raw.get("start_time"))
    if start is None:
        return None, "Preferred slot start time must look like 10:00."
    count = raw.get("slot_count", 1)
    if count in (None, ""):
        count = 1
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_PREFERRED_SLOT_COUNT:
        return None, f"Number of slots must be between 1 and {MAX_PREFERRED_SLOT_COUNT}."
    master_id = raw.get("slot_master")
    if master_id in (None, ""):
        master_id = None
    elif isinstance(master_id, bool) or not str(master_id).isdigit():
        return None, "slot_master must be a slot id."
    else:
        master = SlotMaster.objects.filter(pk=int(master_id), equipment=equipment).only("id").first()
        if master is None:
            return None, "That slot does not belong to this equipment."
        master_id = master.pk
    return {"weekday": weekday, "start_time": start, "slot_count": count, "slot_master_id": master_id}, None


def clean_if_slot_taken(raw):
    value = str(raw or IF_SLOT_TAKEN_ASK).strip()
    if value not in IF_SLOT_TAKEN_CHOICES:
        return None, "if_slot_taken must be one of: " + ", ".join(IF_SLOT_TAKEN_CHOICES) + "."
    return value, None


def apply_preference_fields(template, preferred, if_slot_taken, consent):
    """Set the template's preference columns; return an error string or None. Does not save."""
    if preferred is None:
        template.preferred_weekday = None
        template.preferred_start_time = None
        template.preferred_slot_count = None
        template.preferred_slot_master_id = None
    else:
        template.preferred_weekday = preferred["weekday"]
        template.preferred_start_time = preferred["start_time"]
        template.preferred_slot_count = preferred["slot_count"]
        template.preferred_slot_master_id = preferred["slot_master_id"]
    if if_slot_taken in AUTO_NEXT_MODES:
        already_consented = template.if_slot_taken == if_slot_taken and template.if_slot_taken_consented_at
        if consent is not True and not already_consented:
            return (
                "Tick the consent box to let the portal automatically book the next available slot "
                "(and charge your wallet) when your preferred slot is taken."
            )
        if not already_consented:
            template.if_slot_taken_consented_at = timezone.now()
    else:
        template.if_slot_taken_consented_at = None
    template.if_slot_taken = if_slot_taken
    return None


def has_preferred_slot(template) -> bool:
    return template.preferred_weekday is not None and template.preferred_start_time is not None


def template_books_any_slots(template) -> bool:
    options = template.options if isinstance(template.options, dict) else {}
    return options.get("book_any_available_slots") is True


def effective_if_slot_taken(template) -> str:
    """The "if my slot is taken" choice a template really uses.

    A template offers one fallback. "Book any free slots" wins over the preferred-slot fallback (the book
    request skips the latter whenever the former is on), and the automatic modes need a preferred slot and
    consent. Older templates saved with both keep working this way without rewriting their rows.
    """
    mode = template.if_slot_taken or IF_SLOT_TAKEN_ASK
    if (
        mode not in AUTO_NEXT_MODES
        or not template.if_slot_taken_consented_at
        or not has_preferred_slot(template)
        or template_books_any_slots(template)
    ):
        return IF_SLOT_TAKEN_ASK
    return mode


def serialize_preference(template) -> dict:
    start = template.preferred_start_time
    preferred = None
    if template.preferred_weekday is not None and start is not None:
        preferred = {
            "weekday": template.preferred_weekday,
            "weekday_name": WEEKDAY_NAMES[template.preferred_weekday],
            "start_time": start.strftime("%H:%M"),
            "slot_count": template.preferred_slot_count or 1,
            "slot_master": template.preferred_slot_master_id,
        }
    mode = effective_if_slot_taken(template)
    consented = template.if_slot_taken_consented_at if mode in AUTO_NEXT_MODES else None
    return {
        "preferred_slot": preferred,
        "if_slot_taken": mode,
        "if_slot_taken_consented_at": consented.isoformat() if consented else None,
    }


# --- booking window and candidate slots --------------------------------------------------------


def _user_type(user):
    return getattr(user, "user_type", None) or UserType.STUDENT


def _is_admin(user) -> bool:
    return _user_type(user) in UserType.get_admin_panel_codes()


def booking_window(equipment, user, *, now=None) -> BookingWindow:
    """Dates this user may currently book (mirrors the weekly slots API window)."""
    from .api_views import (
        get_equipment_slot_window_reference_config,
        get_internal_slot_window_date_bounds,
        get_slot_window_reference_datetime_for_local_week,
    )

    now = timezone.localtime(now or timezone.now())
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    if _is_admin(user):
        return BookingWindow(today, monday + timedelta(days=13), None)
    if UserType.is_external_user(_user_type(user)):
        from .external_slot_quota import ExternalSlotQuotaService

        lo, hi, before = ExternalSlotQuotaService.get_external_slot_window_date_bounds(equipment, at=now)
        if lo is None or hi is None:
            lo, hi, before = monday + timedelta(days=7), monday + timedelta(days=13), False
    else:
        lo, hi, before = get_internal_slot_window_date_bounds(equipment, now)
        if lo is None or hi is None:
            return BookingWindow(today, monday + timedelta(days=13), None)
    opens_at = None
    if before:
        rw, rt = get_equipment_slot_window_reference_config(equipment)
        opens_at = get_slot_window_reference_datetime_for_local_week(now, rw, rt)
    return BookingWindow(max(lo, today), hi, opens_at)


def _slot_bookable_for(slot, equipment, user, *, is_admin: bool, is_external: bool) -> bool:
    from .mode_utils import bypasses_multimode_restrictions, equipment_bookable_on_date, family_slots_overlap_conflict
    from .slot_department_access import slot_allows_internal_user
    from .slot_utils import SlotAvailabilityChecker

    if not slot.start_datetime or not slot.end_datetime:
        return False
    if slot.status != SlotStatus.AVAILABLE:
        return False
    start_local = timezone.localtime(slot.start_datetime)
    end_local = timezone.localtime(slot.end_datetime)
    if not is_admin:
        if not SlotAvailabilityChecker.is_slot_available(slot):
            return False
        time_from = getattr(equipment, "weekly_view_time_from", None)
        time_to = getattr(equipment, "weekly_view_time_to", None)
        if time_from is not None and start_local.time() < time_from:
            return False
        if time_to is not None and end_local.time() > time_to:
            return False
        if not is_external and not slot_allows_internal_user(slot, user, equipment):
            return False
    if not bypasses_multimode_restrictions(user):
        ok, _err = equipment_bookable_on_date(equipment, slot.date, start_local.time())
        if not ok:
            return False
        if family_slots_overlap_conflict(equipment, slot.start_datetime, slot.end_datetime, exclude_slot_ids=[slot.id]):
            return False
    return True


def _bookable_candidates(equipment, user, *, date_from, date_to, start_after=None):
    from .slot_department_access import filter_queryset_for_home_department

    if date_from > date_to:
        return []
    is_admin = _is_admin(user)
    is_external = UserType.is_external_user(_user_type(user))
    qs = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        status=SlotStatus.AVAILABLE,
        date__gte=date_from,
        date__lte=date_to,
        start_datetime__gt=timezone.now(),
    )
    if start_after is not None:
        qs = qs.filter(start_datetime__gte=start_after)
    if not is_admin:
        qs = filter_queryset_for_home_department(
            qs, user=user, equipment=equipment, is_admin=False, is_external=is_external
        )
    qs = qs.order_by("start_datetime", "id")[:_CANDIDATE_SCAN_LIMIT]
    return [s for s in qs if _slot_bookable_for(s, equipment, user, is_admin=is_admin, is_external=is_external)]


def _minutes(slots) -> int:
    return int(sum((s.end_datetime - s.start_datetime).total_seconds() for s in slots) // 60)


def _day_successors(equipment, date_from, date_to) -> dict[int, int]:
    """Slot id -> id of the next slot on the same date, whatever its status.

    Consecutive means "next row of the day", as on the booking page: equipment whose slots have breaks
    between them (09:30-11:00, 11:30-13:00, ...) still books runs across the break.
    """
    successors = {}
    prev_id = prev_date = None
    rows = (
        DailySlot.objects.filter(slot_master__equipment=equipment, date__gte=date_from, date__lte=date_to)
        .order_by("date", "start_datetime", "id")
        .values_list("id", "date")
    )
    for slot_id, day in rows:
        if day == prev_date:
            successors[prev_id] = slot_id
        prev_id, prev_date = slot_id, day
    return successors


def _consecutive_runs(candidates, slot_count, successors, total_minutes=None):
    """Non-overlapping runs of ``slot_count`` adjacent free slots on one date (optionally same total minutes)."""
    by_id = {s.id: s for s in candidates}
    used = set()
    runs = []
    for first in candidates:
        if first.id in used:
            continue
        run = [first]
        while len(run) < slot_count:
            nxt = by_id.get(successors.get(run[-1].id))
            if nxt is None or nxt.id in used:
                break
            run.append(nxt)
        if len(run) == slot_count and (total_minutes is None or _minutes(run) == total_minutes):
            runs.append(run)
            used.update(s.id for s in run)
    return runs


def find_equivalent_runs(equipment, user, *, anchor_start, slot_count, total_minutes, mode, limit=1, now=None):
    """Free runs equivalent to the requested one, starting at/after ``anchor_start``, earliest first."""
    window = booking_window(equipment, user, now=now)
    if mode == IF_SLOT_TAKEN_SAME_DAY:
        day = timezone.localtime(anchor_start).date()
        date_from, date_to = max(day, window.min_date), min(day, window.max_date)
    else:
        date_from, date_to = window.min_date, window.max_date
    candidates = _bookable_candidates(
        equipment, user, date_from=date_from, date_to=date_to, start_after=anchor_start
    )
    if not candidates:
        return []
    successors = _day_successors(equipment, date_from, date_to)
    return _consecutive_runs(candidates, slot_count, successors, total_minutes)[:limit]


def requested_run_shape(slot_ids, equipment):
    """(start, end, slot_count, total_minutes) of the slots the user selected, whatever their status now."""
    slots = list(
        DailySlot.objects.filter(id__in=list(slot_ids), slot_master__equipment=equipment).order_by("start_datetime")
    )
    if not slots or len(slots) != len(set(slot_ids)) or any(not s.start_datetime or not s.end_datetime for s in slots):
        return None
    return slots[0].start_datetime, slots[-1].end_datetime, len(slots), _minutes(slots)


def lock_equivalent_run(equipment, user, *, shape, mode):
    """Inside the caller's transaction: claim the first equivalent run whose rows are all still free.

    ``skip_locked`` keeps concurrent fallbacks from queueing behind each other or picking the same rows;
    only the rows of the chosen run are locked.
    """
    anchor_start, _end, slot_count, total_minutes = shape
    for run in find_equivalent_runs(
        equipment, user, anchor_start=anchor_start, slot_count=slot_count,
        total_minutes=total_minutes, mode=mode, limit=_LOCK_ATTEMPTS,
    ):
        ids = [s.id for s in run]
        locked = list(
            DailySlot.objects.select_for_update(skip_locked=True)
            .filter(id__in=ids, status=SlotStatus.AVAILABLE)
            .order_by("start_datetime")
        )
        if len(locked) == len(ids):
            return locked
    return None


def template_fallback_mode(request, equipment, booking_user):
    """The consented auto-next mode of the user's own template named in this book request, if any.

    ``use_template_slot_fallback: false`` means the user picked another "if my slots are taken" choice
    on the booking page for this booking.
    """
    data = request.data or {}
    if data.get("use_template_slot_fallback") is False:
        return None
    raw = data.get("booking_template_id")
    if raw in (None, "") or isinstance(raw, bool) or not str(raw).isdigit():
        return None
    if booking_user is None or booking_user.pk != request.user.pk:
        return None
    template = (
        BookingInputTemplate.objects.filter(pk=int(raw), user=request.user, equipment=equipment)
        .only(
            "if_slot_taken", "if_slot_taken_consented_at", "preferred_weekday", "preferred_start_time", "options",
        )
        .first()
    )
    if template is None:
        return None
    mode = effective_if_slot_taken(template)
    return mode if mode in AUTO_NEXT_MODES else None


def _fmt_range(start, end) -> str:
    s, e = timezone.localtime(start), timezone.localtime(end)
    return f"{s.strftime('%a %d %b')}, {s.strftime('%H:%M')}–{e.strftime('%H:%M')}"


def describe_fallback(shape, booked_slots, mode) -> dict:
    anchor_start, anchor_end, _count, _minutes_ = shape
    start, end = booked_slots[0].start_datetime, booked_slots[-1].end_datetime
    return {
        "mode": mode,
        "requested_start": anchor_start.isoformat(),
        "requested_end": anchor_end.isoformat(),
        "booked_start": start.isoformat(),
        "booked_end": end.isoformat(),
        "slot_ids": [s.id for s in booked_slots],
        "message": (
            f"Your selected slot ({_fmt_range(anchor_start, anchor_end)}) was just taken by another user, so, as your "
            f"template allows, the next available slot was booked instead: {_fmt_range(start, end)}."
        ),
    }


def _run_payload(run) -> dict:
    start, end = run[0].start_datetime, run[-1].end_datetime
    return {
        "date": run[0].date.isoformat(),
        "start_datetime": start.isoformat(),
        "end_datetime": end.isoformat(),
        "slot_ids": [s.id for s in run],
        "label": _fmt_range(start, end),
    }


def nearest_alternatives(equipment, user, *, anchor_start, slot_count, total_minutes, now=None, limit=ALTERNATIVES_LIMIT):
    """Equivalent free runs anywhere in the open window, nearest to the preferred start first."""
    window = booking_window(equipment, user, now=now)
    candidates = _bookable_candidates(equipment, user, date_from=window.min_date, date_to=window.max_date)
    if not candidates:
        return []
    successors = _day_successors(equipment, window.min_date, window.max_date)
    runs = _consecutive_runs(candidates, slot_count, successors, total_minutes)
    runs.sort(key=lambda r: (abs((r[0].start_datetime - anchor_start).total_seconds()), r[0].start_datetime))
    return [_run_payload(r) for r in runs[:limit]]


def attach_slot_taken_alternatives(request, equipment_id, response):
    """On a failed template booking whose selected slots are now taken, add nearby equivalent slots."""
    try:
        if int(getattr(response, "status_code", 0)) != 400 or not isinstance(getattr(response, "data", None), dict):
            return
        data = request.data or {}
        if data.get("booking_template_id") in (None, ""):
            return
        slot_ids = [int(x) for x in (data.get("slot_ids") or []) if str(x).strip().isdigit()]
        if not slot_ids:
            return
        from .models import Equipment

        equipment = Equipment.objects.filter(pk=equipment_id).first()
        if equipment is None:
            return
        shape = requested_run_shape(slot_ids, equipment)
        if shape is None:
            return
        if not DailySlot.objects.filter(id__in=slot_ids).exclude(status=SlotStatus.AVAILABLE).exists():
            return
        anchor_start, _end, slot_count, total_minutes = shape
        response.data["slot_taken"] = True
        response.data["slot_alternatives"] = nearest_alternatives(
            equipment, request.user, anchor_start=anchor_start, slot_count=slot_count, total_minutes=total_minutes
        )
    except Exception:
        import logging

        logging.getLogger(__name__).exception("attach_slot_taken_alternatives failed equipment=%s", equipment_id)


# --- resolving a template's preference on load -------------------------------------------------


def _combine_local(day: date, at: time) -> datetime:
    return timezone.make_aware(datetime.combine(day, at), timezone.get_current_timezone())


def _next_weekday_on_or_after(day: date, weekday: int) -> date:
    return day + timedelta(days=(weekday - day.weekday()) % 7)


def resolve_preferred_slot(template, user, *, slot_count=None, now=None) -> dict:
    """Map the template's weekday/time to concrete slots in the user's open booking window."""
    pref = serialize_preference(template)
    base = {"has_preference": pref["preferred_slot"] is not None, "if_slot_taken": pref["if_slot_taken"]}
    if not base["has_preference"]:
        return base
    equipment = template.equipment
    weekday = template.preferred_weekday
    at = template.preferred_start_time.replace(second=0, microsecond=0)
    count = slot_count or template.preferred_slot_count or 1
    now = timezone.localtime(now or timezone.now())
    window = booking_window(equipment, user, now=now)
    label_time = at.strftime("%H:%M")
    base.update(
        preferred_slot=pref["preferred_slot"],
        slot_count=count,
        window={
            "min_date": window.min_date.isoformat(),
            "max_date": window.max_date.isoformat(),
            "opens_at": window.opens_at.isoformat() if window.opens_at else None,
        },
    )

    target = _next_weekday_on_or_after(window.min_date, weekday)
    if target == now.date() and _combine_local(target, at) <= now:
        target += timedelta(days=7)
    if target > window.max_date:
        upcoming = target
        if window.opens_at:
            when = timezone.localtime(window.opens_at)
            msg = (
                f"Your preferred slot ({WEEKDAY_NAMES[weekday]} {label_time}) is next on {upcoming.strftime('%a %d %b')}, "
                f"which opens for booking on {when.strftime('%a %d %b at %I:%M %p').replace(' 0', ' ')}. "
                "Load the template again once booking opens to select it."
            )
        else:
            msg = (
                f"Your preferred slot ({WEEKDAY_NAMES[weekday]} {label_time}) on {upcoming.strftime('%a %d %b')} "
                "is outside the dates you can book right now."
            )
        base.update(status="not_open", date=upcoming.isoformat(), message=msg, alternatives=[])
        return base

    day_slots = list(
        DailySlot.objects.filter(slot_master__equipment=equipment, date=target).order_by("start_datetime", "id")
    )
    start_idx = None
    if template.preferred_slot_master_id:
        start_idx = next((i for i, s in enumerate(day_slots) if s.slot_master_id == template.preferred_slot_master_id), None)
    if start_idx is None:
        start_idx = next(
            (
                i for i, s in enumerate(day_slots)
                if s.start_datetime and timezone.localtime(s.start_datetime).time().replace(second=0, microsecond=0) == at
            ),
            None,
        )
    run = day_slots[start_idx:start_idx + count] if start_idx is not None else []
    anchor_start = run[0].start_datetime if run else _combine_local(target, at)
    total_minutes = _minutes(run) if len(run) == count else None
    preferred_label = f"{WEEKDAY_NAMES[weekday]} {target.strftime('%d %b')} at {label_time}"
    base.update(date=target.isoformat(), start_datetime=anchor_start.isoformat())

    def _alternatives():
        minutes = total_minutes or count * int(getattr(equipment, "slot_duration_minutes", None) or 60)
        return nearest_alternatives(
            equipment, user, anchor_start=anchor_start, slot_count=count, total_minutes=minutes, now=now
        )

    if len(run) < count:
        base.update(
            status="no_matching_slot",
            message=(
                f"This equipment has no {count}-slot run starting {preferred_label} any more "
                "(its slot timings may have changed). Pick a slot below or edit the template."
            ),
            alternatives=_alternatives(),
        )
        return base

    is_admin = _is_admin(user)
    is_external = UserType.is_external_user(_user_type(user))
    if all(_slot_bookable_for(s, equipment, user, is_admin=is_admin, is_external=is_external) for s in run):
        base.update(
            status="available",
            slot_ids=[s.id for s in run],
            end_datetime=run[-1].end_datetime.isoformat(),
            message=f"Your preferred slot, {_fmt_range(run[0].start_datetime, run[-1].end_datetime)}, is selected.",
            alternatives=[],
        )
        return base

    message = f"Your preferred slot, {_fmt_range(run[0].start_datetime, run[-1].end_datetime)}, is already booked or unavailable."
    auto_next = None
    mode = effective_if_slot_taken(template)
    if mode in AUTO_NEXT_MODES:
        nxt = find_equivalent_runs(
            equipment, user, anchor_start=anchor_start, slot_count=count,
            total_minutes=total_minutes, mode=mode, now=now,
        )
        if nxt:
            auto_next = _run_payload(nxt[0])
            message += (
                f" As your template allows, the next available slot ({auto_next['label']}) is selected; "
                "if that is also taken when you click Book, the next free one after it is booked automatically."
            )
        else:
            message += " No later free slot matches your template's automatic-booking choice right now."
    base.update(status="occupied", message=message, auto_next=auto_next, alternatives=_alternatives())
    return base
