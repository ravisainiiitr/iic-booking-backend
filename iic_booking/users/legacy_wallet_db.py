"""Read-only access to legacy MySQL wallet data (admin.users + user_wallet)."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings

from .legacy_wallet_import import normalize_emp_id

logger = logging.getLogger(__name__)


class LegacyWalletDbError(Exception):
    """Base error for legacy DB access."""


class LegacyWalletNotConfigured(LegacyWalletDbError):
    """LEGACY_MYSQL_* settings are missing."""


class LegacyWalletConnectionError(LegacyWalletDbError):
    """Could not connect or query the legacy database."""


def parse_legacy_mysql_overrides(raw: Any) -> Optional[Dict[str, Any]]:
    """Parse optional legacy_mysql object from an API request body."""
    if not raw or not isinstance(raw, dict):
        return None
    host = (raw.get("host") or "").strip()
    user = (raw.get("user") or "").strip()
    database = (raw.get("database") or "").strip() or "admin"
    port_raw = raw.get("port")
    port = None
    if port_raw not in (None, ""):
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise ValueError("Invalid legacy_mysql.port.")
    password = raw.get("password")
    if password is None:
        password = ""
    overrides: Dict[str, Any] = {"database": database}
    if host:
        overrides["host"] = host
    if user:
        overrides["user"] = user
    if port is not None:
        overrides["port"] = port
    if password != "":
        overrides["password"] = password
    return overrides or None


def resolve_legacy_mysql_config(overrides: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Merge request overrides with Django settings."""
    overrides = overrides or {}
    host = (overrides.get("host") or settings.LEGACY_MYSQL_HOST or "").strip()
    user = (overrides.get("user") or settings.LEGACY_MYSQL_USER or "").strip()
    database = (overrides.get("database") or settings.LEGACY_MYSQL_DATABASE or "admin").strip()
    port = overrides.get("port", settings.LEGACY_MYSQL_PORT or 3306)
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = 3306
    if "password" in overrides and overrides.get("password") != "":
        password = overrides["password"]
    else:
        password = settings.LEGACY_MYSQL_PASSWORD or ""

    if not host or not user or not database:
        raise LegacyWalletNotConfigured(
            "Legacy MySQL is not configured. Set LEGACY_MYSQL_HOST, LEGACY_MYSQL_USER, "
            "LEGACY_MYSQL_PASSWORD, and LEGACY_MYSQL_DATABASE in the server environment (.env)."
        )
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
    }


def is_legacy_mysql_configured(overrides: Optional[Dict[str, Any]] = None) -> bool:
    try:
        resolve_legacy_mysql_config(overrides)
        return True
    except LegacyWalletNotConfigured:
        return False


def _emp_id_lookup_variants(raw: str) -> List[str]:
    variants: set[str] = set()
    s = (raw or "").strip()
    if s:
        variants.add(s)
    normalized = normalize_emp_id(s)
    if normalized:
        variants.add(normalized)
        if normalized.isdigit():
            variants.add(normalized.zfill(6))
    return [v for v in variants if v]


def _connect(mysql_config: Optional[Dict[str, Any]] = None):
    try:
        config = resolve_legacy_mysql_config(mysql_config)
    except LegacyWalletNotConfigured:
        raise

    try:
        import pymysql
    except ImportError as e:
        raise LegacyWalletConnectionError("PyMySQL is not installed.") from e

    try:
        return pymysql.connect(
            host=config["host"],
            port=config["port"],
            user=config["user"],
            password=config["password"],
            database=config["database"],
            connect_timeout=10,
            read_timeout=30,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
        )
    except Exception as e:
        logger.exception("Legacy MySQL connection failed")
        raise LegacyWalletConnectionError(f"Could not connect to legacy database: {e}") from e


def fetch_legacy_wallet_by_emp_id(
    emp_id: str,
    *,
    mysql_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Return legacy user + wallet row for emp_id, or None if not found.

    Matches TRIM(emp_id) against common variants (raw, normalized, zero-padded).
    """
    variants = _emp_id_lookup_variants(emp_id)
    if not variants:
        return None

    placeholders = ", ".join(["%s"] * len(variants))
    sql = f"""
        SELECT
            u.id AS legacy_user_id,
            TRIM(u.emp_id) AS emp_id,
            u.email AS email,
            uw.balance AS balance
        FROM users u
        INNER JOIN user_wallet uw ON uw.user_id = u.id
        WHERE TRIM(u.emp_id) IN ({placeholders})
        LIMIT 2
    """

    conn = None
    try:
        conn = _connect(mysql_config)
        with conn.cursor() as cursor:
            cursor.execute(sql, variants)
            rows = cursor.fetchall()
    except LegacyWalletDbError:
        raise
    except Exception as e:
        logger.exception("Legacy MySQL query failed for emp_id=%s", emp_id)
        raise LegacyWalletConnectionError(f"Legacy database query failed: {e}") from e
    finally:
        if conn is not None:
            conn.close()

    if not rows:
        return None
    if len(rows) > 1:
        raise LegacyWalletConnectionError(
            f"Multiple legacy users match emp_id {emp_id!r}. Resolve duplicates in legacy DB first."
        )

    row = rows[0]
    balance_raw = row.get("balance")
    try:
        balance = Decimal(str(balance_raw)).quantize(Decimal("0.01"))
    except Exception:
        balance = None

    return {
        "legacy_user_id": row.get("legacy_user_id"),
        "emp_id": (row.get("emp_id") or "").strip(),
        "email": (row.get("email") or "").strip(),
        "balance": str(balance) if balance is not None else str(balance_raw),
        "balance_valid": balance is not None,
    }


def _row_to_legacy_wallet_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    balance_raw = row.get("balance")
    try:
        balance = Decimal(str(balance_raw)).quantize(Decimal("0.01"))
        balance_str = str(balance)
    except Exception:
        balance_str = str(balance_raw)
    return {
        "legacy_user_id": row.get("legacy_user_id"),
        "emp_id": (row.get("emp_id") or "").strip(),
        "name": (row.get("name") or "").strip(),
        "email": (row.get("email") or "").strip(),
        "balance": balance_str,
    }


def fetch_all_legacy_wallets_nonzero(
    *,
    mysql_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return all legacy users with non-zero wallet balance."""
    sql = """
        SELECT
            u.id AS legacy_user_id,
            TRIM(u.emp_id) AS emp_id,
            u.name AS name,
            u.email AS email,
            uw.balance AS balance
        FROM users u
        INNER JOIN user_wallet uw ON uw.user_id = u.id
        WHERE uw.balance <> 0
          AND u.emp_id IS NOT NULL
          AND TRIM(u.emp_id) <> ''
        ORDER BY u.name ASC, TRIM(u.emp_id) ASC
    """

    conn = None
    try:
        conn = _connect(mysql_config)
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
    except LegacyWalletDbError:
        raise
    except Exception as e:
        logger.exception("Legacy MySQL list query failed")
        raise LegacyWalletConnectionError(f"Legacy database query failed: {e}") from e
    finally:
        if conn is not None:
            conn.close()

    items = [_row_to_legacy_wallet_summary(row) for row in rows]
    total = Decimal("0.00")
    for item in items:
        try:
            total += Decimal(item["balance"])
        except Exception:
            pass

    cfg = resolve_legacy_mysql_config(mysql_config)
    return {
        "row_count": len(items),
        "total_balance": str(total.quantize(Decimal("0.01"))),
        "legacy_mysql_host": cfg["host"],
        "legacy_mysql_database": cfg["database"],
        "rows": items,
    }
