"""Phase 10E — READ-ONLY legacy booking datetime sanity report."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.utils import timezone

from iic_booking.users.legacy_ledger.datetime_contract import (
    contract_approval_status,
    datetime_contract_ui_payload,
    load_datetime_contract,
)
from iic_booking.users.legacy_ledger.schema_gate import safe_portal_migration_state
from iic_booking.users.legacy_ledger.reader import (
    OldMySQLConnectionError,
    OldMySQLNotConfigured,
    OldMySQLReader,
)

# Flag durations above this for operator review (not auto-reject).
SUSPICIOUS_DURATION_MINUTES = 24 * 60  # 24h
EXTREME_DURATION_MINUTES = 7 * 24 * 60  # 7 days


def validate_legacy_datetime_readonly() -> dict[str, Any]:
    """Inspect production legacy MySQL booking datetime fields — no writes."""
    contract = load_datetime_contract()
    ui = datetime_contract_ui_payload(contract)
    try:
        state, _ = safe_portal_migration_state()
        window_start = getattr(state, "migration_start_at", None)
        window_end = getattr(state, "migration_window_end_at", None)
    except Exception:  # noqa: BLE001 — pre-migration schema may lack window columns
        window_start = None
        window_end = None

    try:
        reader = OldMySQLReader()
    except OldMySQLNotConfigured as exc:
        return {
            "ok": False,
            "error": str(exc),
            "datetime_contract": ui,
            "audit_mode": "READ_ONLY",
        }

    try:
        with reader:
            stats = reader.fetchone(
                """
                SELECT
                  COUNT(*) AS total_bookings,
                  SUM(CASE WHEN `booking_date` IS NULL THEN 1 ELSE 0 END) AS null_booking_date,
                  SUM(CASE WHEN `time_required` IS NULL THEN 1 ELSE 0 END) AS null_time_required,
                  SUM(CASE WHEN `time_required` < 0 THEN 1 ELSE 0 END) AS negative_time_required,
                  SUM(CASE WHEN `time_required` = 0 THEN 1 ELSE 0 END) AS zero_duration,
                  SUM(CASE WHEN `time_required` > %s THEN 1 ELSE 0 END) AS extremely_large_duration,
                  MIN(`time_required`) AS min_time_required,
                  MAX(`time_required`) AS max_time_required,
                  COUNT(DISTINCT `time_required`) AS distinct_time_required
                FROM `booking`
                """,
                (EXTREME_DURATION_MINUTES,),
            ) or {}

            suspicious = reader.fetchall(
                """
                SELECT `time_required`, COUNT(*) AS row_count
                FROM `booking`
                WHERE `time_required` > %s OR `time_required` = 0
                GROUP BY `time_required`
                ORDER BY row_count DESC
                LIMIT 30
                """,
                (SUSPICIOUS_DURATION_MINUTES,),
            )

            duration_distribution = reader.fetchall(
                """
                SELECT `time_required`, COUNT(*) AS row_count
                FROM `booking`
                GROUP BY `time_required`
                ORDER BY row_count DESC
                LIMIT 25
                """
            )

            in_window = 0
            outside_window = 0
            crossing_midnight = 0
            if window_start and window_end:
                ws = window_start
                we = window_end
                if timezone.is_naive(ws):
                    ws = timezone.make_aware(ws, timezone.get_current_timezone())
                if timezone.is_naive(we):
                    we = timezone.make_aware(we, timezone.get_current_timezone())
                rows = reader.fetchall(
                    """
                    SELECT `booking_date`, `time_required`
                    FROM `booking`
                    WHERE `booking_date` IS NOT NULL
                    """
                )
                for row in rows:
                    bd = row.get("booking_date")
                    tr = row.get("time_required")
                    if not isinstance(bd, datetime):
                        continue
                    start = bd
                    if timezone.is_naive(start):
                        start = timezone.make_aware(start, timezone.get_current_timezone())
                    try:
                        minutes = int(tr) if tr is not None else 0
                    except (TypeError, ValueError):
                        minutes = 0
                    end = start + timedelta(minutes=minutes)
                    if start.date() != end.date() and minutes > 0:
                        crossing_midnight += 1
                    if ws <= start < we:
                        in_window += 1
                    else:
                        outside_window += 1
    except OldMySQLConnectionError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "datetime_contract": ui,
            "audit_mode": "READ_ONLY",
        }

    return {
        "ok": True,
        "audit_mode": "READ_ONLY",
        "datetime_contract": ui,
        "contract_approval_status": contract_approval_status(contract),
        "migration_window": {
            "start": window_start.isoformat() if window_start else None,
            "end": window_end.isoformat() if window_end else None,
            "configured": bool(window_start and window_end),
        },
        "totals": {
            "total_bookings": int(stats.get("total_bookings") or 0),
            "null_booking_date": int(stats.get("null_booking_date") or 0),
            "null_time_required": int(stats.get("null_time_required") or 0),
            "negative_time_required": int(stats.get("negative_time_required") or 0),
            "zero_duration": int(stats.get("zero_duration") or 0),
            "extremely_large_duration": int(stats.get("extremely_large_duration") or 0),
            "bookings_crossing_midnight": crossing_midnight,
            "bookings_in_migration_window": in_window,
            "bookings_outside_migration_window": outside_window,
        },
        "duration_distribution_top": duration_distribution,
        "suspicious_durations": suspicious,
        "suspicious_threshold_minutes": SUSPICIOUS_DURATION_MINUTES,
        "extreme_threshold_minutes": EXTREME_DURATION_MINUTES,
        "note": (
            "Suspicious durations are reported for operator review; legitimate long bookings "
            "are not automatically rejected."
        ),
    }
