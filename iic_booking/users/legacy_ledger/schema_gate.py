"""Portal-migration schema readiness helpers (pre-0101–0105 safe).

Production may run application code that references users.0102 fields/tables
before Migrate Production is authorized. Introspect schema without ORM SELECT
of missing columns (which would ProgrammingError and poison ATOMIC_REQUESTS).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from django.db import connection
from django.db.utils import ProgrammingError
from rest_framework import status
from rest_framework.response import Response

# users.0102_legacy_equipment_booking_bridge
_0102_STATE_COLUMNS = (
    "migration_start_at",
    "migration_window_end_at",
    "booking_migration_mode",
    "new_portal_url",
)
_0102_TABLES = (
    "users_legacyequipmentmapping",
    "users_legacybookingmigrationbatch",
    "users_legacybookingblock",
)
_0101_TABLES = ("users_migrationbookingsettlement",)
_0103_TABLES = ("users_migrationnotificationbatch",)
_0105_TABLES = ("users_legacyequipmentcapacitysplit",)


def _pg_columns(table: str) -> set[str]:
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_name = %s
            """,
            [table],
        )
        return {str(r[0]) for r in cur.fetchall()}


def _table_names() -> set[str]:
    return set(connection.introspection.table_names())


@lru_cache(maxsize=1)
def portal_bridge_schema_status_cached() -> tuple[Any, ...]:
    """Cached tuple form for lru_cache; use portal_bridge_schema_status()."""
    tables = _table_names()
    state_cols = _pg_columns("users_portalmigrationstate") if "users_portalmigrationstate" in tables else set()
    missing_cols = [c for c in _0102_STATE_COLUMNS if c not in state_cols]
    missing_tables = [t for t in _0102_TABLES if t not in tables]
    pending: list[str] = []
    if any(t not in tables for t in _0101_TABLES):
        pending.append("0101")
    if missing_cols or missing_tables:
        pending.append("0102")
    if any(t not in tables for t in _0103_TABLES):
        pending.append("0103")
    if "0102" in pending or "users_legacybookingblock" not in tables:
        pending.append("0104")
    elif "users_legacybookingblock" in tables:
        block_cols = _pg_columns("users_legacybookingblock")
        if "legacy_user_id" not in block_cols:
            pending.append("0104")
    if any(t not in tables for t in _0105_TABLES):
        pending.append("0105")
    ready = not pending and not missing_cols and not missing_tables
    return (
        ready,
        tuple(pending),
        tuple(missing_cols),
        tuple(missing_tables),
        "migration_start_at" in state_cols,
        "users_legacyequipmentmapping" in tables,
        "users_legacybookingblock" in tables,
    )


def clear_portal_bridge_schema_cache() -> None:
    portal_bridge_schema_status_cached.cache_clear()


def portal_bridge_schema_status() -> dict[str, Any]:
    ready, pending, missing_cols, missing_tables, has_start, has_eq_map, has_blocks = (
        portal_bridge_schema_status_cached()
    )
    return {
        "ready": bool(ready),
        "code": "OK" if ready else "SCHEMA_PENDING",
        "gate": "OK" if ready else "OPERATOR_REQUIRED",
        "pending_migrations": list(pending),
        "missing_columns": list(missing_cols),
        "missing_tables": list(missing_tables),
        "has_migration_start_at": bool(has_start),
        "has_legacy_equipment_mapping_table": bool(has_eq_map),
        "has_legacy_booking_block_table": bool(has_blocks),
        "detail": (
            "Portal migration bridge schema applied."
            if ready
            else (
                "users.0101–0105 not fully applied on this database. "
                "Application code is deployed; Migrate Production is still OPERATOR_REQUIRED. "
                "Do not invent migration_start_at or run manual ALTER."
            )
        ),
        "migrate_authorized": False,
        "migrate_executed": False,
    }


def schema_pending_payload(**extra: Any) -> dict[str, Any]:
    base = portal_bridge_schema_status()
    payload = {
        "ok": False,
        "code": "SCHEMA_PENDING",
        # Not the datetime-contract gate — schema migrate authorization only.
        "approval_status": "SCHEMA_PENDING",
        "gate": "OPERATOR_REQUIRED",
        "gate_kind": "SCHEMA_MIGRATE",
        "schema": base,
        "results": [],
        "table": [],
        "count": 0,
        "t0_executed": False,
        "message": (
            "Equipment mapping and legacy booking discovery require users.0101–0105. "
            "Datetime contract approval is separate and does not unlock these pages."
        ),
        "operator_next_actions": [
            "Authorize and run Migrate Production for users.0101–0105 (confirm_migrate=MIGRATE)",
            "Then configure migration_start_at / migration_window_end_at (operator-supplied ISO)",
            "Approve datetime contract on /admin/portal-migration (file-based; does not require 0102)",
            "Create ACTIVE capacity split for TG/DTA (1→A+B TIME_BAND_FOLD) when needed",
            "Only then run read-only discovery / use equipment + legacy booking pages",
        ],
    }
    payload.update(extra)
    return payload


def schema_pending_response(**extra: Any) -> Response:
    """HTTP 503 — expected until schema migrate; never a bare 500 ProgrammingError."""
    return Response(schema_pending_payload(**extra), status=status.HTTP_503_SERVICE_UNAVAILABLE)


def bridge_schema_ready_for_orm() -> bool:
    """True when LegacyEquipmentMapping / window columns exist for full ORM use."""
    st = portal_bridge_schema_status()
    return bool(st["has_migration_start_at"] and st["has_legacy_equipment_mapping_table"])


def _fallback_state(**overrides: Any):
    base = {
        "phase": "PREPARATION",
        "end_user_booking_enabled": True,
        "booking_opens_at": None,
        "migration_start_at": None,
        "migration_window_end_at": None,
        "booking_migration_mode": "NORMAL",
        "new_portal_url": "",
        "booking_lock_message": "",
        "last_sync_error": "",
        "incremental_sync_enabled": False,
        "legacy_ledger_frozen": False,
        "last_sync_at": None,
        "last_sync_batch": "",
        "last_sync_duration_ms": 0,
        "last_sync_imported_count": 0,
        "last_sync_processed_count": 0,
        "sync_runs_total": 0,
        "sync_failures_total": 0,
        "transactions_imported_total": 0,
        "last_wallet_txn_watermark": 0,
        "pk": None,
    }
    base.update(overrides)
    return type("PortalMigrationStateFallback", (), base)()


def safe_portal_migration_state():
    """
    Load PortalMigrationState without selecting missing 0102 columns.
    Returns (state_or_namespace, schema_status).
    """
    from iic_booking.users.models.portal_migration import PortalMigrationState

    st = portal_bridge_schema_status()
    if st["has_migration_start_at"]:
        return PortalMigrationState.get_solo(), st

    # Pre-0102: SELECT only columns that exist (never migration_start_at).
    try:
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT phase, end_user_booking_enabled, booking_opens_at,
                       booking_lock_message, last_sync_error, incremental_sync_enabled,
                       legacy_ledger_frozen, last_sync_at, last_sync_batch,
                       last_sync_duration_ms, last_sync_imported_count,
                       last_sync_processed_count, sync_runs_total, sync_failures_total,
                       transactions_imported_total, last_wallet_txn_watermark, id
                FROM users_portalmigrationstate
                WHERE singleton_key = %s
                LIMIT 1
                """,
                ["default"],
            )
            row = cur.fetchone()
    except ProgrammingError:
        return _fallback_state(), st

    if not row:
        return _fallback_state(), st

    return (
        _fallback_state(
            phase=row[0] or "PREPARATION",
            end_user_booking_enabled=bool(row[1]) if row[1] is not None else True,
            booking_opens_at=row[2],
            booking_lock_message=row[3] or "",
            last_sync_error=row[4] or "",
            incremental_sync_enabled=bool(row[5]),
            legacy_ledger_frozen=bool(row[6]),
            last_sync_at=row[7],
            last_sync_batch=row[8] or "",
            last_sync_duration_ms=row[9] or 0,
            last_sync_imported_count=row[10] or 0,
            last_sync_processed_count=row[11] or 0,
            sync_runs_total=row[12] or 0,
            sync_failures_total=row[13] or 0,
            transactions_imported_total=row[14] or 0,
            last_wallet_txn_watermark=row[15] or 0,
            pk=row[16],
            migration_start_at=None,
            migration_window_end_at=None,
            booking_migration_mode="NORMAL",
            new_portal_url="",
        ),
        st,
    )