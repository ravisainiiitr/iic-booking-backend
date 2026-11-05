"""Silent hours: an Officer In Charge can stop users calling an instrument's staff outside office hours, at weekends and on
holidays. Off until switched on. A mode with no rule of its own follows its base equipment. Staff of the instrument are
never held back: this is about users calling in, and calls already ringing or in progress carry on.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from django.utils import timezone

from .models import CallSilentHours

LOOK_AHEAD_DAYS = 60
DEFAULTS = {"enabled": False, "office_start": "09:00", "office_end": "17:30", "silent_weekends": True, "silent_holidays": True}


def _now() -> datetime:
    return timezone.now()


def rule_for(equipment) -> CallSilentHours | None:
    rule = CallSilentHours.objects.filter(equipment_id=equipment.pk).first()
    parent_id = getattr(equipment, "parent_equipment_id", None)
    if rule is None and parent_id:
        rule = CallSilentHours.objects.filter(equipment_id=parent_id).first()
    return rule


def _holidays(start, end) -> set:
    from iic_booking.equipment.models import Holiday

    return set(Holiday.objects.filter(date__range=(start, end), is_active=True).values_list("date", flat=True))


def _day_is_silent(rule: CallSilentHours, day, holidays: set) -> bool:
    return (rule.silent_weekends and day.weekday() >= 5) or (rule.silent_holidays and day in holidays)


def is_silent(rule: CallSilentHours | None, now: datetime | None = None) -> bool:
    if rule is None or not rule.enabled:
        return False
    local = timezone.localtime(now or _now())
    if _day_is_silent(rule, local.date(), _holidays(local.date(), local.date())):
        return True
    return not rule.office_start <= local.time() < rule.office_end


def next_open(rule: CallSilentHours, now: datetime | None = None) -> datetime | None:
    """When office hours next begin; None if no working day is left in the next two months."""
    local = timezone.localtime(now or _now())
    holidays = _holidays(local.date(), local.date() + timedelta(days=LOOK_AHEAD_DAYS))
    for offset in range(LOOK_AHEAD_DAYS + 1):
        day = local.date() + timedelta(days=offset)
        if _day_is_silent(rule, day, holidays):
            continue
        start = timezone.make_aware(datetime.combine(day, rule.office_start))
        if start > local:
            return start
    return None


def _clock(value) -> str:
    return value.strftime("%I:%M %p").lstrip("0")


def describe_block(rule: CallSilentHours, now: datetime | None = None) -> dict:
    opens = next_open(rule, now)
    if opens is None:
        return {"active": True, "until": None, "message": "Calls to the lab are off at the moment. Raise a support ticket instead."}
    local = timezone.localtime(opens)
    when = f"{local:%a} {local.day} {local:%b} at {_clock(local.time())}"
    return {"active": True, "until": opens.isoformat(), "message": f"Calls to the lab are off until {when}."}


def blocking(equipment, now: datetime | None = None) -> dict | None:
    """The block in force for users calling this instrument's staff right now, if any."""
    rule = rule_for(equipment)
    return describe_block(rule, now) if is_silent(rule, now) else None
