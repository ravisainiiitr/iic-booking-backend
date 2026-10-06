"""Batch faculty wallet sync from legacy MySQL: the daily scheduled run and admin-triggered runs.

Each user goes through the same path as the faculty login sync (sync_faculty_wallet_from_legacy),
so entries share the login migration_id markers and stay idempotent with login syncs. Differences:
deductions that would take the IIC sub-wallet below zero are refused and reported, one failing
user never stops the run, and the summary carries user ids and amounts only (no names or emails).
Runs only while faculty_wallet_sync_window_open() (Main Administrator deadline).
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.core.cache import cache
from django.db import DatabaseError
from django.utils import timezone

from iic_booking.users.legacy_ledger.booking_lock import faculty_wallet_sync_window_open
from iic_booking.users.legacy_ledger.faculty_login_wallet_sync import (
    _mysql_configured,
    sync_faculty_wallet_from_legacy,
)
from iic_booking.users.legacy_ledger.reader import OldMySQLReader
from iic_booking.users.models import User, UserType
from iic_booking.users.models.portal_migration import PortalMigrationState

logger = logging.getLogger(__name__)

FLOOR = Decimal("0.00")
LOCK_KEY = "faculty-wallet-batch-sync-lock"
LOCK_SECONDS = 2 * 60 * 60
MAX_CHANGES_STORED = 500


def _money(value) -> Decimal:
    return Decimal(str(value or "0")).quantize(Decimal("0.01"))


def _faculty_queryset(user_ids: list[int] | None):
    qs = User.objects.filter(user_type=UserType.FACULTY).exclude(emp_id__isnull=True).exclude(emp_id="")
    if user_ids:
        qs = qs.filter(pk__in=user_ids)
    return qs.order_by("pk")


def _new_summary(*, apply: bool, trigger: str) -> dict:
    return {
        "trigger": trigger,
        "mode": "apply" if apply else "dry_run",
        "status": "completed",
        "started_at": timezone.now().isoformat(),
        "finished_at": None,
        "checked": 0,
        "in_sync": 0,
        "credits": {"count": 0, "total": "0.00"},
        "debits": {"count": 0, "total": "0.00"},
        "ledger_rows_imported": 0,
        "blocked_below_zero": [],
        "skipped": {},
        "skipped_admin_mapped_user_ids": [],
        "failed": [],
        "changes": [],
    }


def _add_total(bucket: dict, amount: Decimal) -> None:
    bucket["count"] += 1
    bucket["total"] = str(_money(bucket["total"]) + amount)


def _classify(summary: dict, user_id: int, result: dict, *, apply: bool) -> None:
    if result.get("skipped"):
        reason = str(result.get("reason") or "skipped")
        summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1
        if reason == "admin_mapped_other_legacy_user":
            summary["skipped_admin_mapped_user_ids"].append(user_id)
        return
    if not result.get("ok"):
        summary["failed"].append({"user_id": user_id, "reason": str(result.get("reason") or "error")})
        return

    summary["ledger_rows_imported"] += int(result.get("imported") or 0)
    recon = result.get("reconcile") or {}
    delta = _money(recon.get("delta"))
    balance = _money(recon.get("sub_wallet_balance"))
    if recon.get("blocked_below_floor"):
        summary["blocked_below_zero"].append(
            {"user_id": user_id, "delta": str(delta), "iic_balance": str(balance)}
        )
        return
    if delta == 0:
        summary["in_sync"] += 1
        return
    if apply and not recon.get("created"):
        summary["in_sync"] += 1
        return

    before, after = (balance - delta, balance) if apply else (balance, balance + delta)
    _add_total(summary["credits" if delta > 0 else "debits"], abs(delta))
    summary["changes"].append(
        {
            "user_id": user_id,
            "action": "credit" if delta > 0 else "debit",
            "delta": str(delta),
            "iic_balance_before": str(before),
            "iic_balance_after": str(after),
        }
    )


def run_faculty_wallet_batch_sync(
    *,
    apply: bool,
    trigger: str = "manual",
    user_ids: list[int] | None = None,
    reader=None,
) -> dict:
    """Sync every faculty member (or ``user_ids``) with an employee id. Dry run unless ``apply``."""
    summary = _new_summary(apply=apply, trigger=trigger)

    if not faculty_wallet_sync_window_open():
        summary["status"] = "window_closed"
    elif reader is None and not _mysql_configured():
        summary["status"] = "legacy_mysql_not_configured"
    elif apply and not cache.add(LOCK_KEY, summary["started_at"], LOCK_SECONDS):
        summary["status"] = "already_running"
    else:
        try:
            _run_users(summary, apply=apply, user_ids=user_ids, reader=reader)
        finally:
            if apply:
                cache.delete(LOCK_KEY)

    summary["finished_at"] = timezone.now().isoformat()
    return summary


def _run_users(summary: dict, *, apply: bool, user_ids: list[int] | None, reader) -> None:
    owns_reader = reader is None
    try:
        if owns_reader:
            reader = OldMySQLReader()
            reader.connect()
        for user in _faculty_queryset(user_ids).iterator():
            summary["checked"] += 1
            try:
                result = sync_faculty_wallet_from_legacy(user, reader=reader, dry_run=not apply, floor=FLOOR)
            except Exception:  # noqa: BLE001 - one user must never stop the run
                logger.exception("Faculty wallet batch sync crashed for user_id=%s", user.pk)
                result = {"ok": False, "skipped": False, "reason": "error"}
            _classify(summary, user.pk, result, apply=apply)
            if owns_reader and result.get("reason") == "error":
                reader = _reconnect(reader)
    finally:
        if owns_reader and reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass


def _reconnect(reader):
    """A dropped legacy MySQL connection would otherwise fail every remaining user."""
    try:
        reader.close()
    except Exception:  # noqa: BLE001
        pass
    fresh = OldMySQLReader()
    try:
        fresh.connect()
    except Exception:  # noqa: BLE001
        logger.exception("Faculty wallet batch sync: legacy MySQL reconnect failed")
    return fresh


def record_run(summary: dict) -> None:
    """Keep the latest applied run on PortalMigrationState for the Legacy user sync page."""
    stored = dict(summary)
    stored["changes_total"] = len(summary.get("changes") or [])
    stored["changes"] = (summary.get("changes") or [])[:MAX_CHANGES_STORED]
    try:
        PortalMigrationState.objects.get_or_create(singleton_key="default")
        PortalMigrationState.objects.filter(singleton_key="default").update(
            faculty_wallet_last_batch_sync_at=timezone.now(),
            faculty_wallet_last_batch_sync_summary=stored,
        )
    except DatabaseError:
        logger.exception("Faculty wallet batch sync: could not record the run summary")


def summary_log_line(summary: dict) -> str:
    """One line of counts and totals for logs (no names, emails or per-user detail)."""
    return (
        f"trigger={summary['trigger']} mode={summary['mode']} status={summary['status']} "
        f"checked={summary['checked']} in_sync={summary['in_sync']} "
        f"credits={summary['credits']['count']} credit_total={summary['credits']['total']} "
        f"debits={summary['debits']['count']} debit_total={summary['debits']['total']} "
        f"blocked_below_zero={len(summary['blocked_below_zero'])} "
        f"skipped={sum(summary['skipped'].values())} failed={len(summary['failed'])} "
        f"ledger_rows_imported={summary['ledger_rows_imported']}"
    )
