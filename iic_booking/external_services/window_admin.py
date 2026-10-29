"""Main Administrator changes to the external booking window. Every change is made under a row lock on the
configuration, recorded in ``ExternalBookingWindowChange`` with the bookable range before and after, and may not
silently shorten the range: shortening needs explicit confirmation and is refused while any active external
booking falls after the new last date. No change cancels an existing booking."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Optional

from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from iic_booking.department_modules.access import is_main_admin

from . import window as window_mod
from .models import ExternalBookingWindowChange, ExternalBookingWindowConfig, WindowAction

EDITABLE = (
    "enabled",
    "initial_open_at",
    "first_week_start",
    "initial_weeks",
    "auto_expand",
    "expansion_weekday",
    "expansion_time",
    "min_lead_days",
)
MIN_REASON = 10
MAX_WEEKS = 26
ACTIVE_BOOKING_STATUSES = ("PENDING", "PENDING_PAYMENT", "BOOKED", "HOLD")


class WindowAdminError(Exception):
    def __init__(self, message: str, *, status: int = 400, code: str = "invalid", field: str = "", extra=None):
        super().__init__(message)
        self.message, self.status, self.code, self.field = message, status, code, field
        self.extra = extra or {}

    def payload(self) -> dict:
        body = {"error": self.message, "code": self.code}
        if self.field:
            body["field"] = self.field
        body.update(self.extra)
        return body


def require_main_admin(actor) -> None:
    if not is_main_admin(actor):
        raise WindowAdminError(
            "Only the Main Administrator can change the external booking window.", status=403, code="forbidden"
        )


def config_dict(config: ExternalBookingWindowConfig) -> dict:
    return {
        "enabled": config.enabled,
        "initial_open_at": timezone.localtime(config.initial_open_at).isoformat() if config.initial_open_at else None,
        "first_week_start": config.first_week_start.isoformat() if config.first_week_start else None,
        "initial_weeks": config.initial_weeks,
        "auto_expand": config.auto_expand,
        "expansion_weekday": config.expansion_weekday,
        "expansion_time": config.expansion_time.strftime("%H:%M") if config.expansion_time else None,
        "materialized_expansions": config.materialized_expansions,
        "counting_from": timezone.localtime(config.counting_from).isoformat() if config.counting_from else None,
        "manual_weeks": config.manual_weeks,
        "min_lead_days": config.min_lead_days,
        "updated_at": timezone.localtime(config.updated_at).isoformat() if config.updated_at else None,
    }


def _range(config, at) -> dict:
    w = window_mod.compute(config, at)
    return {"enabled": w.enabled, "is_open": w.is_open, "first_date": w.first_date.isoformat() if w.first_date else None,
            "last_date": w.last_date.isoformat() if w.last_date else None}


def _reason(reason) -> str:
    text = (reason or "").strip()
    if len(text) < MIN_REASON:
        raise WindowAdminError(f"Give a reason of at least {MIN_REASON} characters.", field="reason")
    return text[:2000]


def _bool(value, field):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    raise WindowAdminError(f"{field} must be true or false.", field=field)


def _int(value, field, lo, hi):
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise WindowAdminError(f"{field} must be a whole number.", field=field)
    if not lo <= number <= hi:
        raise WindowAdminError(f"{field} must be between {lo} and {hi}.", field=field)
    return number


def _safe_parse(parser, value):
    """Django's parsers return None for a wrong format but raise ValueError for impossible values (25:00)."""
    try:
        return parser(str(value))
    except ValueError:
        return None


def _parse_open_at(value) -> Optional[datetime]:
    if value in (None, ""):
        return None
    parsed = _safe_parse(parse_datetime, value)
    if parsed is None:
        raise WindowAdminError("initial_open_at must be a date and time (ISO 8601).", field="initial_open_at")
    if timezone.is_naive(parsed):  # a naive value is local (IST) time
        parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed


def _parse_monday(value) -> Optional[date]:
    if value in (None, ""):
        return None
    parsed = _safe_parse(parse_date, value)
    if parsed is None:
        raise WindowAdminError("first_week_start must be a date (YYYY-MM-DD).", field="first_week_start")
    if parsed.weekday() != 0:
        raise WindowAdminError("first_week_start must be a Monday.", field="first_week_start")
    return parsed


def _parse_time(value) -> time:
    parsed = _safe_parse(parse_time, value or "")
    if parsed is None:
        raise WindowAdminError("expansion_time must be HH:MM.", field="expansion_time")
    return parsed.replace(second=0, microsecond=0)


def _materialize(config, at) -> None:
    """Fold the expansions counted so far into ``materialized_expansions`` and restart counting at ``at``."""
    config.materialized_expansions = window_mod.counted_expansions(config, at)
    config.counting_from = at


def _apply_fields(config, data: dict, at) -> dict:
    changes = {}
    if "initial_open_at" in data:
        changes["initial_open_at"] = _parse_open_at(data["initial_open_at"])
    if "first_week_start" in data:
        changes["first_week_start"] = _parse_monday(data["first_week_start"])
    if "initial_weeks" in data:
        changes["initial_weeks"] = _int(data["initial_weeks"], "initial_weeks", 1, MAX_WEEKS)
    if "min_lead_days" in data:
        changes["min_lead_days"] = _int(data["min_lead_days"], "min_lead_days", 0, 60)
    if "expansion_weekday" in data:
        changes["expansion_weekday"] = _int(data["expansion_weekday"], "expansion_weekday", 0, 6)
    if "expansion_time" in data:
        changes["expansion_time"] = _parse_time(data["expansion_time"])
    if "auto_expand" in data:
        changes["auto_expand"] = _bool(data["auto_expand"], "auto_expand")
    if "enabled" in data:
        changes["enabled"] = _bool(data["enabled"], "enabled")
    unknown = sorted(set(data) - set(EDITABLE) - {"reason", "confirm_reduction"})
    if unknown:
        raise WindowAdminError(f"These fields cannot be changed here: {', '.join(unknown)}.", field=unknown[0])

    schedule_change = any(
        k in changes and changes[k] != getattr(config, k)
        for k in ("expansion_weekday", "expansion_time", "auto_expand")
    )
    anchor_change = any(
        k in changes and changes[k] != getattr(config, k) for k in ("initial_open_at", "first_week_start")
    )
    if anchor_change:
        # A new opening restarts the count from that opening (a backdated opening counts the missed expansions).
        for k in ("initial_open_at", "first_week_start"):
            if k in changes:
                setattr(config, k, changes[k])
        config.materialized_expansions = 0
        config.counting_from = config.initial_open_at
        config.manual_weeks = 0
    elif schedule_change and config.initial_open_at and at >= config.initial_open_at:
        _materialize(config, at)
    for k, v in changes.items():
        setattr(config, k, v)
    if config.enabled and not window_mod.is_configured(config):
        raise WindowAdminError("Set the opening date and time and the first week before enabling.", field="enabled")
    return changes


def _active_external_bookings_after(last: Optional[date]) -> int:
    from iic_booking.equipment.models import DailySlot
    from iic_booking.users.models.user_type import UserType

    if last is None:
        return 0
    return (
        DailySlot.objects.filter(
            date__gt=last,
            booking__isnull=False,
            booking__status__in=ACTIVE_BOOKING_STATUSES,
            booking__user_type_snapshot__in=list(UserType.get_external_user_codes()),
        )
        .values("booking_id")
        .distinct()
        .count()
    )


def _guard_reduction(before: dict, after: dict, *, confirm: bool) -> None:
    b_last, a_last = before.get("last_date"), after.get("last_date")
    shrinks = bool(before.get("is_open") and b_last and (not after.get("is_open") or (a_last and a_last < b_last)))
    if not shrinks:
        return
    if not confirm:
        raise WindowAdminError(
            "This change closes dates that are open to external users now. Confirm it to continue.",
            code="reduction_needs_confirmation",
            extra={"range_before": before, "range_after": after},
        )
    new_last = date.fromisoformat(a_last) if (after.get("is_open") and a_last) else date.min
    affected = _active_external_bookings_after(new_last)
    if affected:
        raise WindowAdminError(
            f"{affected} active external booking(s) fall after the new last date. "
            "The window cannot be shortened past existing bookings.",
            code="reduction_blocked_by_bookings",
            extra={"affected_bookings": affected, "range_before": before, "range_after": after},
        )


def _record(actor, action, before_cfg, config, range_before, range_after, reason):
    ExternalBookingWindowChange.objects.create(
        actor=actor, action=action, before=before_cfg, after=config_dict(config),
        range_before=range_before, range_after=range_after, reason=reason,
    )


def preview(data: dict, *, at=None) -> dict:
    """The range now and after the proposed change, without saving (Main Administrator UI)."""
    at = at or timezone.now()
    config = ExternalBookingWindowConfig.load()
    before = _range(config, at)
    _apply_fields(config, {k: v for k, v in data.items() if k in EDITABLE}, at)
    forced = window_mod.compute(config, at, force_enabled=True)
    return {
        "range_before": before,
        "range_after": _range(config, at),
        "range_if_enabled": {
            "is_open": forced.is_open,
            "first_date": forced.first_date.isoformat() if forced.first_date else None,
            "last_date": forced.last_date.isoformat() if forced.last_date else None,
            "next_expansion_at": timezone.localtime(forced.next_expansion_at).isoformat()
            if forced.next_expansion_at else None,
        },
    }


def update(actor, data: dict, *, at=None) -> ExternalBookingWindowConfig:
    require_main_admin(actor)
    reason = _reason(data.get("reason"))
    at = at or timezone.now()
    with transaction.atomic():
        config = ExternalBookingWindowConfig.objects.select_for_update().get(pk=ExternalBookingWindowConfig.load().pk)
        before_cfg, range_before = config_dict(config), _range(config, at)
        was_enabled = config.enabled
        changes = _apply_fields(config, data, at)
        if not changes:
            raise WindowAdminError("Nothing to change.", code="nothing_to_change")
        range_after = _range(config, at)
        _guard_reduction(range_before, range_after, confirm=bool(data.get("confirm_reduction")))
        config.updated_by = actor
        config.save()
        action = WindowAction.CONFIGURE
        if config.enabled != was_enabled:
            action = WindowAction.ENABLE if config.enabled else WindowAction.DISABLE
        _record(actor, action, before_cfg, config, range_before, range_after, reason)
    return config


def _simple(actor, reason, at, action, mutate) -> ExternalBookingWindowConfig:
    require_main_admin(actor)
    reason = _reason(reason)
    at = at or timezone.now()
    with transaction.atomic():
        config = ExternalBookingWindowConfig.objects.select_for_update().get(pk=ExternalBookingWindowConfig.load().pk)
        before_cfg, range_before = config_dict(config), _range(config, at)
        mutate(config, at)
        range_after = _range(config, at)
        config.updated_by = actor
        config.save()
        _record(actor, action, before_cfg, config, range_before, range_after, reason)
    return config


def pause(actor, reason, *, at=None):
    def mutate(config, now):
        if not config.auto_expand:
            raise WindowAdminError("Weekly expansion is already paused.", code="already_paused")
        if config.initial_open_at and now >= config.initial_open_at:
            _materialize(config, now)
        config.auto_expand = False

    return _simple(actor, reason, at, WindowAction.PAUSE, mutate)


def resume(actor, reason, *, at=None):
    """Expansions that fell while paused are not caught up: counting restarts now."""

    def mutate(config, now):
        if config.auto_expand:
            raise WindowAdminError("Weekly expansion is not paused.", code="not_paused")
        config.auto_expand = True
        config.counting_from = max(now, config.initial_open_at) if config.initial_open_at else now

    return _simple(actor, reason, at, WindowAction.RESUME, mutate)


def advance(actor, weeks, reason, *, at=None):
    """Open ``weeks`` more weeks now, in addition to the scheduled weekly expansions."""
    weeks = _int(weeks, "weeks", 1, 8)

    def mutate(config, now):
        if not config.enabled or not window_mod.is_configured(config):
            raise WindowAdminError("Enable and configure the window before opening weeks early.", code="not_enabled")
        if config.initial_open_at and now < config.initial_open_at:
            raise WindowAdminError("The window has not opened yet; change the opening instead.", code="not_open")
        config.manual_weeks = int(config.manual_weeks or 0) + weeks

    return _simple(actor, reason, at, WindowAction.ADVANCE, mutate)


def history(limit: int = 50) -> list[dict]:
    rows = ExternalBookingWindowChange.objects.select_related("actor").order_by("-created_at", "-id")[:limit]
    out = []
    for r in rows:
        out.append({
            "id": r.pk,
            "action": r.action,
            "action_label": r.get_action_display(),
            "actor": getattr(r.actor, "email", None),
            "reason": r.reason,
            "range_before": r.range_before,
            "range_after": r.range_after,
            "before": r.before,
            "after": r.after,
            "created_at": timezone.localtime(r.created_at).isoformat(),
        })
    return out

