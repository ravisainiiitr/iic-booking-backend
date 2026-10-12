"""
Both SRIC mailbox readers together: the SRIC portal's Wallet_Recharge.csv (no-reply@sric) and the cash-book
(bills@sric). They run on the beat schedule (every SRIC_MAILBOX_POLL_SECONDS) and on the Refresh button
(Main Administrator / Accounts In-charge), behind the readers' own locks and a short global Refresh cooldown.
Each reader stores the IMAP UIDs it has handled, so an email is never processed twice.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)

SRIC_CSV_TASK = "users.scan_sric_wallet_recharge_mailbox"
CASHBOOK_TASK = "users.read_sric_cashbook_mailbox"
COOLDOWN_KEY = "sric_mailbox_refresh_cooldown"
LAST_REFRESH_KEY = "sric_mailbox_last_refresh"
REFRESH_MAX_MESSAGES = 10


class RefreshCooldown(Exception):
    def __init__(self, retry_after: int):
        super().__init__(f"Checked moments ago. Please wait {retry_after} seconds.")
        self.retry_after = retry_after


def poll_seconds() -> int:
    return max(60, int(getattr(settings, "SRIC_MAILBOX_POLL_SECONDS", 300) or 300))


def cooldown_seconds() -> int:
    return max(0, int(getattr(settings, "SRIC_MAILBOX_REFRESH_COOLDOWN_SECONDS", 45) or 0))


def can_refresh(user) -> bool:
    from iic_booking.users.models import UserType

    return bool(getattr(user, "is_authenticated", False)) and (
        getattr(user, "is_superuser", False) or getattr(user, "user_type", None) in (UserType.ADMIN, UserType.FINANCE)
    )


# --- Schedule ---------------------------------------------------------------------------------------------------


def _interval_seconds(task) -> int | None:
    interval = getattr(task, "interval", None)
    if interval is None:
        return None
    unit = {"days": 86400, "hours": 3600, "minutes": 60, "seconds": 1, "microseconds": 0}.get(interval.period, 0)
    return interval.every * unit or None


def sync_schedules(*, seconds: int | None = None, apply: bool = False) -> list[dict[str, Any]]:
    """Put both reader tasks on an every-``seconds`` interval (default SRIC_MAILBOX_POLL_SECONDS)."""
    from django_celery_beat.models import IntervalSchedule, PeriodicTask

    seconds = seconds or poll_seconds()
    out = []
    for name in (SRIC_CSV_TASK, CASHBOOK_TASK):
        task = PeriodicTask.objects.select_related("interval").filter(task=name).first()
        before = _interval_seconds(task) if task else None
        entry = {"task": name, "present": task is not None, "enabled": getattr(task, "enabled", None),
                 "before_seconds": before, "after_seconds": before, "changed": False}
        if task is not None and before != seconds and apply:
            every, _ = IntervalSchedule.objects.get_or_create(every=seconds, period=IntervalSchedule.SECONDS)
            task.interval = every
            task.crontab = None
            task.solar = None
            task.clocked = None
            task.save()
            entry.update(after_seconds=seconds, changed=True)
        elif task is not None and before != seconds:
            entry["after_seconds"] = f"{seconds} (dry run)"
        out.append(entry)
    return out


def _next_run(task, last: datetime | None) -> datetime | None:
    every = _interval_seconds(task) if task else None
    if not every or not getattr(task, "enabled", False):
        return None
    base = getattr(task, "last_run_at", None) or last
    if base is None:
        return timezone.now()
    nxt = base + timedelta(seconds=every)
    return max(nxt, timezone.now())


def _parse(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# --- Status -----------------------------------------------------------------------------------------------------


def status() -> dict[str, Any]:
    from django_celery_beat.models import PeriodicTask

    from iic_booking.users import sric_wallet_recharge as sric
    from iic_booking.users import wallet_cashbook_mailbox as cashbook
    from iic_booking.users.models.sric_wallet_recharge import SricWalletRechargeSettings
    from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

    tasks = {t.task: t for t in PeriodicTask.objects.select_related("interval").filter(task__in=[SRIC_CSV_TASK, CASHBOOK_TASK])}
    sric_cfg = SricWalletRechargeSettings.get_singleton()
    cash_last = cashbook.last_run()
    readers = [
        {
            "key": "sric_csv",
            "label": "SRIC portal recharges (Wallet_Recharge.csv)",
            "enabled": bool(sric_cfg.scan_enabled),
            "last_checked_at": _iso(sric_cfg.last_scan_at),
            "last_result": {k: v for k, v in (sric_cfg.last_scan_result or {}).items() if k not in ("review_row_ids", "messages")},
            "task": tasks.get(SRIC_CSV_TASK),
            "running": cache.get(sric.LOCK_KEY) is not None,
        },
        {
            "key": "cashbook",
            "label": "SRIC cash-book (bills@sric)",
            "enabled": bool(WalletSricSettings.get_singleton().auto_read_cashbook_mailbox),
            "last_checked_at": cash_last.get("at"),
            "last_result": {k: v for k, v in cash_last.items() if k != "at"},
            "task": tasks.get(CASHBOOK_TASK),
            "running": cache.get(cashbook.LOCK_KEY) is not None,
        },
    ]
    for r in readers:
        task = r.pop("task")
        nxt = _next_run(task, _parse(r["last_checked_at"])) if r["enabled"] else None
        r.update(
            schedule_seconds=_interval_seconds(task) if task else None,
            schedule_enabled=bool(getattr(task, "enabled", False)),
            next_check_at=_iso(nxt),
        )
    checked = [d for d in (_parse(r["last_checked_at"]) for r in readers) if d]
    upcoming = [d for d in (_parse(r["next_check_at"]) for r in readers) if d]
    nxt = min(upcoming) if upcoming else None
    expires = cache.get(COOLDOWN_KEY)
    return {
        "poll_seconds": poll_seconds(),
        "cooldown_seconds": cooldown_seconds(),
        "cooldown_remaining": max(0, int((expires or 0) - time.time())),
        "running": any(r["running"] for r in readers),
        "last_checked_at": _iso(max(checked)) if checked else None,
        "next_check_at": _iso(nxt),
        "next_check_in_seconds": max(0, int((nxt - timezone.now()).total_seconds())) if nxt else None,
        "readers": readers,
        "last_refresh": cache.get(LAST_REFRESH_KEY) or None,
    }


# --- Refresh ----------------------------------------------------------------------------------------------------


def summarize(sric_result: dict[str, Any], cash_result: dict[str, Any]) -> dict[str, Any]:
    statuses = sric_result.get("statuses") or {}
    sric_matched = int(statuses.get("credited", 0)) + int(statuses.get("awaiting_credit", 0))
    sric_unmatched = int(statuses.get("needs_review", 0)) + int(statuses.get("failed", 0))
    cash_rows = int(cash_result.get("rows_stored", 0) or 0)
    cash_matched = int(cash_result.get("requests_matched", 0) or 0)
    readers = {
        "sric_csv": {
            "status": sric_result.get("status"),
            "new_emails": int(sric_result.get("messages_read", 0) or 0),
            "rows_parsed": int(sric_result.get("rows_new", 0) or 0),
            "matched": sric_matched,
            "credited": int(sric_result.get("credited", 0) or 0),
            "unmatched": sric_unmatched,
            "duplicates": int(statuses.get("duplicate", 0)),
            "rechecked": int(sric_result.get("rechecked", 0) or 0),
            "error": sric_result.get("error", ""),
        },
        "cashbook": {
            "status": cash_result.get("status"),
            "new_emails": int(cash_result.get("messages_read", 0) or 0),
            "rows_parsed": cash_rows,
            "matched": cash_matched,
            "unmatched": max(0, cash_rows - cash_matched),
            "error": "; ".join(cash_result.get("errors") or [])[:300],
        },
    }
    totals = {k: sum(r[k] for r in readers.values()) for k in ("new_emails", "rows_parsed", "matched", "unmatched")}
    return {"totals": totals, "readers": readers, "message": _message(totals, readers)}


def _message(totals: dict[str, int], readers: dict[str, dict[str, Any]]) -> str:
    states = {r["status"] for r in readers.values()}
    if states <= {"already_running"}:
        return "A mailbox check is already running. The lists will update in a moment."
    if "error" in states:
        return "Could not reach the portal mailbox. Please try again in a few minutes."
    if not totals["new_emails"]:
        return "Mailbox checked: no new SRIC emails."
    parts = [f"{totals['new_emails']} new email(s)", f"{totals['rows_parsed']} row(s) read", f"{totals['matched']} matched"]
    if totals["unmatched"]:
        parts.append(f"{totals['unmatched']} not matched (see the review list)")
    return "Mailbox checked: " + ", ".join(parts) + "."


def refresh(user) -> dict[str, Any]:
    """Read both SRIC mailboxes now (same locks as the schedule). Raises RefreshCooldown when checked moments ago."""
    from iic_booking.users.sric_wallet_recharge import scan_mailbox
    from iic_booking.users.wallet_cashbook_mailbox import read_cashbook_mailbox

    wait = cooldown_seconds()
    now = time.time()
    if wait and not cache.add(COOLDOWN_KEY, now + wait, wait):
        expires = cache.get(COOLDOWN_KEY) or now + wait
        raise RefreshCooldown(max(1, int(expires - now)))
    try:
        sric_result = scan_mailbox(trigger="admin-refresh", max_messages=REFRESH_MAX_MESSAGES)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SRIC mailbox refresh: Wallet_Recharge.csv reader failed (%s)", type(exc).__name__)
        sric_result = {"status": "error", "error": "Could not read the SRIC portal emails."}
    try:
        cash_result = read_cashbook_mailbox(max_messages=REFRESH_MAX_MESSAGES, trigger="refresh")
    except Exception as exc:  # noqa: BLE001
        logger.warning("SRIC mailbox refresh: cash-book reader failed (%s)", type(exc).__name__)
        cash_result = {"status": "error", "errors": ["Could not read the cash-book emails."]}
    result = summarize(sric_result, cash_result)
    cache.set(
        LAST_REFRESH_KEY,
        {"at": timezone.now().isoformat(), "by_user_id": getattr(user, "pk", None), **result},
        None,
    )
    logger.info("SRIC mailbox refresh by user %s: %s", getattr(user, "pk", None), result["totals"])
    return {"result": result, "status": status()}
