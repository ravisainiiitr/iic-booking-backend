"""Staging legacy MySQL snapshot reader — same interface as OldMySQLReader / FakeOldMySQLReader.

Loads JSON from LEGACY_MYSQL_STAGING_FIXTURE_PATH. Never connects to MySQL.
Never available as a silent production fallback.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils.dateparse import parse_datetime

from iic_booking.users.legacy_ledger.fake_reader import FakeOldMySQLReader


def _normalize_txn(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["amount"] = str(Decimal(str(out.get("amount", "0"))))
    cd = out.get("create_date")
    if isinstance(cd, str):
        parsed = parse_datetime(cd)
        out["create_date"] = parsed or datetime.fromisoformat(cd.replace("Z", "+00:00"))
    return out


def legacy_fixture_mode_enabled() -> bool:
    if not bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False)):
        return False
    env = (getattr(settings, "DEPLOYMENT_ENVIRONMENT", "") or "").upper()
    if env != "STAGING":
        raise ImproperlyConfigured(
            "LEGACY_MYSQL_STAGING_FIXTURE_MODE only allowed when DEPLOYMENT_ENVIRONMENT=STAGING."
        )
    return True


def load_staging_legacy_snapshot(path: str | Path | None = None) -> FakeOldMySQLReader:
    raw_path = path or getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_PATH", "") or ""
    if not raw_path:
        raise ImproperlyConfigured(
            "LEGACY_MYSQL_STAGING_FIXTURE_PATH is required when fixture mode is enabled."
        )
    p = Path(raw_path)
    if not p.is_file():
        raise ImproperlyConfigured(f"Legacy staging fixture not found: {p}")
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    users = list(data.get("users") or [])
    wallets_list = list(data.get("wallets") or [])
    transactions = [_normalize_txn(t) for t in (data.get("transactions") or [])]
    wallets = {
        int(w["user_id"]): {
            **w,
            "balance": Decimal(str(w.get("balance", "0"))),
        }
        for w in wallets_list
    }
    reader = FakeOldMySQLReader(users=users, wallets=wallets, transactions=transactions)
    reader.probe = {
        "ok": True,
        "database": "staging_fixture",
        "configured_host": "fixture",
        "configured_port": 0,
        "configured_database": "staging_fixture",
        "mode": "STAGING_FIXTURE",
        "row_counts": {
            "users": len(users),
            "user_wallet": len(wallets),
            "wallet_transactions": len(transactions),
            "bookings": len(data.get("bookings") or []),
        },
    }
    # Attach bookings for booking migration dry-run (not part of wallet reader contract).
    reader.bookings = list(data.get("bookings") or [])  # type: ignore[attr-defined]
    return reader


def get_legacy_reader(*, require_real: bool = False):
    """Return fixture reader or real OldMySQLReader.

    Hard rules:
    - Never silently fall back from a failed/missing REAL MySQL config to fixtures.
    - If OLD_MYSQL_HOST is configured (real mode intent) OR require_real=True
      OR REAL_INTEGRATION_ENABLED=true, fixture mode is refused.
    - Fixture mode only when explicitly enabled AND no real MySQL host is set
      AND REAL_INTEGRATION_ENABLED is false.
    """
    from iic_booking.users.legacy_ledger.real_integration_guards import real_integration_enabled

    host = (getattr(settings, "OLD_MYSQL_HOST", None) or "").strip()
    fixture_wanted = False
    try:
        fixture_wanted = legacy_fixture_mode_enabled()
    except ImproperlyConfigured:
        raise

    real_intent = bool(require_real or host or real_integration_enabled())

    if real_intent:
        if fixture_wanted:
            raise ImproperlyConfigured(
                "REAL legacy MySQL is intended (OLD_MYSQL_HOST set and/or "
                "REAL_INTEGRATION_ENABLED=true / require_real) while "
                "LEGACY_MYSQL_STAGING_FIXTURE_MODE=true. Refusing silent fixture "
                "substitution. Set LEGACY_MYSQL_STAGING_FIXTURE_MODE=false for live sync."
            )
        from iic_booking.users.legacy_ledger.real_integration_guards import (
            assert_real_legacy_mysql_ready,
        )

        assert_real_legacy_mysql_ready()
        from iic_booking.users.legacy_ledger.reader import OldMySQLReader

        return OldMySQLReader()

    if fixture_wanted:
        return load_staging_legacy_snapshot()

    from iic_booking.users.legacy_ledger.reader import OldMySQLNotConfigured, OldMySQLReader

    try:
        return OldMySQLReader()
    except OldMySQLNotConfigured:
        raise ImproperlyConfigured(
            "Legacy MySQL is not configured. For REAL integration set OLD_MYSQL_* "
            "and REAL_INTEGRATION_ENABLED=true. "
            "For staging fixture qualification set LEGACY_MYSQL_STAGING_FIXTURE_MODE=true "
            "(STAGING only). No silent fallback."
        ) from None
