"""Enrich legacy wallet ledger rows for Wallet UI display.

Equipment name and booked-by are not stored on immutable ledger rows; derive them
from description text and (when available) live read-only legacy MySQL created_by.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

logger = logging.getLogger(__name__)

# "Paid towards Electron Probe Micro-Analysis (EPMA) Booking."
_PAID_TOWARDS_RE = re.compile(
    r"Paid\s+towards\s+(.+?)\s+Booking\.?\s*$",
    re.I | re.S,
)
_BOOKING_HASH_RE = re.compile(
    r"Booking\s*#\s*(.+?)\s*[-–]\s*(.+?)(?:\s*\(|$)",
    re.I,
)
_FOR_EQUIPMENT_RE = re.compile(
    r"(?:for|towards)\s+([A-Za-z0-9][A-Za-z0-9 ./\-()[\]]+?)\s+(?:booking|slot)",
    re.I,
)


def parse_legacy_equipment_name(description: str | None) -> str | None:
    """Best-effort equipment label from a legacy wallet_transactions.description."""
    desc = (description or "").strip()
    if not desc:
        return None
    m = _PAID_TOWARDS_RE.search(desc)
    if m:
        name = m.group(1).strip(" .-")
        return name or None
    m = _BOOKING_HASH_RE.search(desc)
    if m:
        name = (m.group(2) or m.group(1) or "").strip(" .-")
        return name or None
    m = _FOR_EQUIPMENT_RE.search(desc)
    if m:
        name = m.group(1).strip(" .-")
        return name or None
    return None


def _chunks(items: list[int], size: int = 250) -> Iterable[list[int]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def lookup_legacy_created_by(
    source_transaction_ids: list[int],
) -> dict[int, dict[str, str | None]]:
    """
    Map legacy wallet_transactions.id -> {name, email} using created_by.

    created_by is typically a users.id FK in the old portal; if the join fails or
    created_by is a plain name string, fall back to that string as the display name.
    Never raises — returns {} on any MySQL / config error.
    """
    ids = sorted({int(x) for x in source_transaction_ids if x is not None})
    if not ids:
        return {}
    try:
        from iic_booking.users.legacy_ledger.reader import (
            OldMySQLNotConfigured,
            OldMySQLReader,
        )
    except Exception:
        return {}

    out: dict[int, dict[str, str | None]] = {}
    reader = None
    try:
        reader = OldMySQLReader()
        reader.connect()
        for chunk in _chunks(ids):
            placeholders = ",".join(["%s"] * len(chunk))
            sql = (
                "SELECT wt.id AS txn_id, wt.created_by, "
                "u.name AS user_name, u.email AS user_email "
                f"FROM wallet_transactions wt "
                f"LEFT JOIN users u ON u.id = wt.created_by "
                f"WHERE wt.id IN ({placeholders})"
            )
            try:
                rows = reader.fetchall(sql, tuple(chunk))
            except Exception:
                # created_by may not be numeric — fall back to selecting raw created_by only
                sql_fallback = (
                    f"SELECT id AS txn_id, created_by FROM wallet_transactions "
                    f"WHERE id IN ({placeholders})"
                )
                try:
                    rows = reader.fetchall(sql_fallback, tuple(chunk))
                except Exception as exc:
                    logger.warning(
                        "Legacy created_by lookup failed: %s",
                        type(exc).__name__,
                    )
                    return out
            for row in rows or []:
                txn_id = int(row["txn_id"])
                name = (row.get("user_name") or "").strip() or None
                email = (row.get("user_email") or "").strip() or None
                if not name:
                    raw = row.get("created_by")
                    if raw is not None and str(raw).strip():
                        # Non-numeric string stored in created_by
                        raw_s = str(raw).strip()
                        if not raw_s.isdigit():
                            name = raw_s
                if name or email:
                    out[txn_id] = {"name": name, "email": email}
    except OldMySQLNotConfigured:
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("Legacy created_by enrichment skipped: %s", type(exc).__name__)
        return {}
    finally:
        if reader is not None:
            try:
                reader.close()
            except Exception:
                pass
    return out
