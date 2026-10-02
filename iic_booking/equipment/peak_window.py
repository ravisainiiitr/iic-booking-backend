"""
Peak booking window around the weekly slot opening.

The window for one slot-opening schedule (weekday + time, per equipment or the global
internal slot window) runs from (opening - lead minutes) to (opening + trail minutes),
end exclusive. The site is "in peak" when any active equipment is in its window.

Everything here is read on hot paths (middleware, status endpoint), so settings and
schedules are cached in-process for a few seconds on top of the shared cache, and both
are invalidated when an admin edits them.
"""
from __future__ import annotations

import functools
import hashlib
import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Iterable, Optional

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

PEAK_EXTERNAL_PAUSED_CODE = "peak_window_external_paused"

DEFAULT_LEAD_MINUTES = 5
DEFAULT_TRAIL_MINUTES = 15
DEFAULT_EXTERNAL_NOTICE_MINUTES = 30

SETTINGS_CACHE_KEY = "peak_window:settings:v1"
SCHEDULES_CACHE_KEY = "peak_window:schedules:v1"
SHARED_CACHE_TTL_SECONDS = 60
LOCAL_CACHE_TTL_SECONDS = 5

_local_lock = threading.Lock()
_local_cache: dict[str, tuple[float, object]] = {}


@dataclass(frozen=True)
class PeakSettings:
    enabled: bool = True
    lead_minutes: int = DEFAULT_LEAD_MINUTES
    trail_minutes: int = DEFAULT_TRAIL_MINUTES
    block_external_users: bool = True
    external_notice_minutes: int = DEFAULT_EXTERNAL_NOTICE_MINUTES
    defer_background_tasks: bool = True

    def as_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "lead_minutes": self.lead_minutes,
            "trail_minutes": self.trail_minutes,
            "block_external_users": self.block_external_users,
            "external_notice_minutes": self.external_notice_minutes,
            "defer_background_tasks": self.defer_background_tasks,
        }


@dataclass(frozen=True)
class PeakWindow:
    opening_at: datetime
    starts_at: datetime
    ends_at: datetime

    def contains(self, at: datetime) -> bool:
        return self.starts_at <= at < self.ends_at

    def as_dict(self) -> dict:
        return {
            "opening_at": self.opening_at.isoformat(),
            "starts_at": self.starts_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
        }


def _now() -> datetime:
    return timezone.now()


def _local_get(key: str):
    with _local_lock:
        hit = _local_cache.get(key)
    if hit and hit[0] > _time.monotonic():
        return hit[1]
    return None


def _local_set(key: str, value, ttl: float = LOCAL_CACHE_TTL_SECONDS) -> None:
    with _local_lock:
        _local_cache[key] = (_time.monotonic() + ttl, value)


def invalidate_peak_window_cache() -> None:
    """Drop cached settings and schedules (call after admin edits)."""
    with _local_lock:
        _local_cache.clear()
    try:
        cache.delete_many([SETTINGS_CACHE_KEY, SCHEDULES_CACHE_KEY])
    except Exception:
        logger.warning("peak window cache delete failed", exc_info=True)


def _cached(key: str, loader):
    value = _local_get(key)
    if value is not None:
        return value
    try:
        value = cache.get(key)
    except Exception:
        value = None
    if value is None:
        value = loader()
        try:
            cache.set(key, value, SHARED_CACHE_TTL_SECONDS)
        except Exception:
            pass
    _local_set(key, value)
    return value


def get_or_create_peak_setting_row():
    from .models import PeakWindowSetting

    obj = PeakWindowSetting.objects.order_by("pk").first()
    if obj is None:
        obj = PeakWindowSetting.objects.create()
    return obj


def _load_settings() -> PeakSettings:
    try:
        from .models import PeakWindowSetting

        # Savepoint: a missing table (code deployed before the migration) must not abort
        # the caller's transaction under ATOMIC_REQUESTS.
        with transaction.atomic():
            obj = PeakWindowSetting.objects.order_by("pk").first()
    except Exception:
        logger.warning("peak window settings unavailable; using defaults", exc_info=True)
        obj = None
    if obj is None:
        return PeakSettings()
    return PeakSettings(
        enabled=bool(obj.enabled),
        lead_minutes=int(obj.lead_minutes),
        trail_minutes=int(obj.trail_minutes),
        block_external_users=bool(obj.block_external_users),
        external_notice_minutes=int(obj.external_notice_minutes),
        defer_background_tasks=bool(obj.defer_background_tasks),
    )


def get_peak_settings() -> PeakSettings:
    return _cached(SETTINGS_CACHE_KEY, _load_settings)


def _load_schedules() -> tuple[tuple[int, str], ...]:
    """Distinct (weekday, "HH:MM:SS") slot openings of active equipment."""
    from .api_views import get_internal_slot_window_setting
    from .models import Equipment, EquipmentStatus

    out: set[tuple[int, str]] = set()
    try:
        with transaction.atomic():
            rows = list(
                Equipment.objects.filter(status=EquipmentStatus.ACTIVE)
                .order_by()
                .values_list("slot_window_reference_weekday", "slot_window_reference_time")
                .distinct()
            )
            uses_global = False
            for weekday, at in rows:
                if weekday is not None and at is not None:
                    out.add((int(weekday), at.strftime("%H:%M:%S")))
                else:
                    uses_global = True
            if uses_global:
                setting = get_internal_slot_window_setting()
                if (
                    setting is not None
                    and setting.reference_weekday is not None
                    and setting.reference_time is not None
                ):
                    out.add((int(setting.reference_weekday), setting.reference_time.strftime("%H:%M:%S")))
    except Exception:
        logger.warning("peak window schedules unavailable", exc_info=True)
        return tuple()
    return tuple(sorted(out))


def get_opening_schedules() -> tuple[tuple[int, time], ...]:
    raw = _cached(SCHEDULES_CACHE_KEY, _load_schedules)
    return tuple((wd, time.fromisoformat(t)) for wd, t in raw)


def _windows_around(
    at: datetime, schedules: Iterable[tuple[int, time]], settings: PeakSettings
) -> list[PeakWindow]:
    tz = timezone.get_current_timezone()
    local = timezone.localtime(at, tz)
    week_monday = local.date() - timedelta(days=local.weekday())
    lead = timedelta(minutes=settings.lead_minutes)
    trail = timedelta(minutes=settings.trail_minutes)
    windows = []
    for weekday, opening_time in schedules:
        for week_offset in (-1, 0, 1):
            day = week_monday + timedelta(days=7 * week_offset + int(weekday))
            opening = timezone.make_aware(datetime.combine(day, opening_time), tz)
            windows.append(PeakWindow(opening_at=opening, starts_at=opening - lead, ends_at=opening + trail))
    windows.sort(key=lambda w: (w.starts_at, w.ends_at))
    return windows


def _merge_containing(windows: list[PeakWindow], at: datetime) -> Optional[PeakWindow]:
    """Union of the windows that contain ``at`` plus any window overlapping that union."""
    current = [w for w in windows if w.contains(at)]
    if not current:
        return None
    start = min(w.starts_at for w in current)
    end = max(w.ends_at for w in current)
    changed = True
    while changed:
        changed = False
        for w in windows:
            if w.starts_at < end and w.ends_at > start and (w.starts_at < start or w.ends_at > end):
                start, end = min(start, w.starts_at), max(end, w.ends_at)
                changed = True
    opening = min(w.opening_at for w in windows if w.starts_at >= start and w.ends_at <= end)
    return PeakWindow(opening_at=opening, starts_at=start, ends_at=end)


def compute_peak_state(
    at: Optional[datetime] = None,
    schedules: Optional[Iterable[tuple[int, time]]] = None,
    settings: Optional[PeakSettings] = None,
) -> dict:
    """Pure computation of the peak state at ``at`` (defaults: now, cached schedules/settings)."""
    at = _now() if at is None else at
    if timezone.is_naive(at):
        at = timezone.make_aware(at, timezone.get_current_timezone())
    settings = get_peak_settings() if settings is None else settings
    schedules = tuple(get_opening_schedules() if schedules is None else schedules)

    state = {
        "enabled": settings.enabled,
        "peak_window_active": False,
        "starts_at": None,
        "ends_at": None,
        "opening_at": None,
        "next_window": None,
        "block_external_users": settings.block_external_users,
        "external_access_paused": False,
        "external_notice_active": False,
        "external_notice_starts_at": None,
        "lead_minutes": settings.lead_minutes,
        "trail_minutes": settings.trail_minutes,
        "server_time": at.isoformat(),
        "_current": None,
        "_next": None,
    }
    if not settings.enabled or not schedules:
        return state

    windows = _windows_around(at, schedules, settings)
    current = _merge_containing(windows, at)
    if current is not None:
        state.update(
            peak_window_active=True,
            starts_at=current.starts_at.isoformat(),
            ends_at=current.ends_at.isoformat(),
            opening_at=current.opening_at.isoformat(),
            external_access_paused=settings.block_external_users,
            _current=current,
        )
    after = current.ends_at if current is not None else at
    upcoming = [w for w in windows if w.starts_at >= after and w.starts_at > at]
    if upcoming:
        nxt = _merge_containing(windows, upcoming[0].starts_at) or upcoming[0]
        state["next_window"] = nxt.as_dict()
        state["_next"] = nxt
        if settings.block_external_users and settings.external_notice_minutes > 0 and current is None:
            notice_start = nxt.starts_at - timedelta(minutes=settings.external_notice_minutes)
            state["external_notice_starts_at"] = notice_start.isoformat()
            state["external_notice_active"] = notice_start <= at < nxt.starts_at
    return state


def public_peak_state(at: Optional[datetime] = None) -> dict:
    state = compute_peak_state(at)
    window = state.get("_current") or state.get("_next")
    state["external_paused_message"] = external_paused_message(window) if window else ""
    return {k: v for k, v in state.items() if not k.startswith("_")}


def is_peak_window_active(at: Optional[datetime] = None) -> bool:
    return bool(compute_peak_state(at)["peak_window_active"])


def current_peak_window(at: Optional[datetime] = None) -> Optional[PeakWindow]:
    return compute_peak_state(at).get("_current")


def _format_clock(dt: datetime) -> str:
    local = timezone.localtime(dt)
    hour = local.hour % 12 or 12
    suffix = "am" if local.hour < 12 else "pm"
    if local.minute:
        return f"{hour}:{local.minute:02d} {suffix}"
    return f"{hour} {suffix}"


def external_paused_message(window: PeakWindow) -> str:
    start = _format_clock(window.starts_at)
    end = _format_clock(window.ends_at)
    day = timezone.localtime(window.opening_at).strftime("%A")
    return (
        "To give IIT Roorkee users a fair chance when new slots open, external access is paused "
        f"from {start} to {end} on {day}s. Please come back after {end}."
    )


# --------------------------------------------------------------------------------------
# External user pause
# --------------------------------------------------------------------------------------

def _external_codes() -> set[str]:
    from iic_booking.users.models.user_type import UserType

    return {str(c).lower() for c in UserType.get_external_user_codes()}


def is_peak_blockable_user(user) -> bool:
    """External, Industry, R&D and other non-IITR end users. Staff and admins never."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return False
    code = str(getattr(user, "user_type", "") or "").strip().lower()
    return code in _external_codes()


def external_pause_payload(window: Optional[PeakWindow]) -> dict:
    message = external_paused_message(window) if window else (
        "External access is paused while new slots open. Please come back in a few minutes."
    )
    return {
        "detail": message,
        "error": message,
        "message": message,
        "code": PEAK_EXTERNAL_PAUSED_CODE,
        "peak_window": window.as_dict() if window else None,
    }


def external_user_paused_window(user, at: Optional[datetime] = None) -> Optional[PeakWindow]:
    """The current window when this user is paused right now, else None."""
    if not is_peak_blockable_user(user):
        return None
    state = compute_peak_state(at)
    if not state["external_access_paused"]:
        return None
    return state["_current"]


def peak_login_refusal(user):
    """DRF Response refusing sign-in for a paused external user, or None."""
    window = external_user_paused_window(user)
    if window is None:
        return None
    from rest_framework import status as drf_status
    from rest_framework.response import Response

    return Response(external_pause_payload(window), status=drf_status.HTTP_403_FORBIDDEN)


# --------------------------------------------------------------------------------------
# Background work deferral
# --------------------------------------------------------------------------------------

def seconds_until_peak_end_for_deferral(at: Optional[datetime] = None) -> int:
    """Seconds until the current window ends when background work should wait, else 0."""
    try:
        settings = get_peak_settings()
        if not settings.defer_background_tasks:
            return 0
        state = compute_peak_state(at, settings=settings)
    except Exception:
        return 0
    current = state.get("_current")
    if current is None:
        return 0
    now = _now() if at is None else at
    return max(1, int((current.ends_at - now).total_seconds()))


def defer_during_peak(task_name: str, *, jitter_seconds: int = 120):
    """
    Wrap a Celery task body: during the peak window, re-queue it for after the window
    (once per task + arguments) instead of running now. Put it under @shared_task.
    """

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            delay = seconds_until_peak_end_for_deferral()
            if not delay:
                return fn(*args, **kwargs)
            call_args = list(args)
            try:
                from celery import Task

                if call_args and isinstance(call_args[0], Task):
                    call_args = call_args[1:]
            except Exception:
                pass
            digest = hashlib.sha1(repr((task_name, call_args, sorted(kwargs.items()))).encode()).hexdigest()[:16]
            countdown = delay + (int(digest, 16) % max(1, jitter_seconds))
            try:
                if cache.add(f"peak_window:deferred:{digest}", 1, timeout=countdown):
                    from celery import current_app

                    current_app.send_task(task_name, args=call_args, kwargs=kwargs, countdown=countdown)
            except Exception:
                # Never drop the work: if it cannot be re-queued, run it now.
                logger.warning("could not re-queue %s after the peak window; running now", task_name, exc_info=True)
                return fn(*args, **kwargs)
            logger.info("Deferred %s for %ss (peak booking window)", task_name, countdown)
            return {"deferred": "peak_window", "retry_in_seconds": countdown}

        return wrapper

    return decorator
