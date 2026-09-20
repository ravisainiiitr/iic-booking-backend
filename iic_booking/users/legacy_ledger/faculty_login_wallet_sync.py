"""Faculty login-time wallet sync from legacy MySQL into IIC SubWallet.

Runs only while now < FACULTY_WALLET_SYNC_CUTOFF (4 Oct 2026 Asia/Kolkata).
Never raises to the caller — login must always succeed.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from django.conf import settings

from iic_booking.users.legacy_ledger.booking_lock import faculty_wallet_sync_window_open
from iic_booking.users.legacy_ledger.importer import import_transaction
from iic_booking.users.legacy_ledger.mapping import (
    MappingRow,
    exact_employee_id,
    upsert_mapping,
)
from iic_booking.users.legacy_ledger.opening_balance import (
    OpeningBalanceError,
    get_iic_department,
    reconcile_legacy_balance_to_subwallet,
)
from iic_booking.users.legacy_ledger.reader import OldMySQLNotConfigured, OldMySQLReader
from iic_booking.users.models import UserType
from iic_booking.users.models.portal_migration import LegacyWalletMappingStatus

logger = logging.getLogger(__name__)

BATCH_PREFIX = "faculty-login-sync"


def _mysql_configured() -> bool:
    if bool(getattr(settings, "LEGACY_MYSQL_STAGING_FIXTURE_MODE", False)):
        return True
    host = (getattr(settings, "OLD_MYSQL_HOST", None) or "").strip()
    user = (getattr(settings, "OLD_MYSQL_USER", None) or "").strip()
    database = (getattr(settings, "OLD_MYSQL_DATABASE", None) or "").strip()
    return bool(host and user and database)


def faculty_login_migration_id(*, user_id: int, employee_id: str) -> str:
    return f"faculty-wallet:{employee_id or user_id}"


def sync_faculty_wallet_from_legacy(
    user,
    *,
    reader: OldMySQLReader | None = None,
) -> dict:
    """
    Import legacy ledger rows for this faculty user and reconcile IIC SubWallet balance.

    Returns a status dict. Safe to call on every faculty login until cutover.
    """
    if getattr(user, "user_type", None) != UserType.FACULTY:
        return {"ok": False, "skipped": True, "reason": "not_faculty"}
    if not faculty_wallet_sync_window_open():
        return {"ok": False, "skipped": True, "reason": "after_cutover"}
    if reader is None and not _mysql_configured():
        return {"ok": False, "skipped": True, "reason": "legacy_mysql_not_configured"}

    emp = exact_employee_id(getattr(user, "emp_id", None))
    if not emp:
        return {"ok": False, "skipped": True, "reason": "missing_emp_id"}

    owns_reader = reader is None
    try:
        if owns_reader:
            reader = OldMySQLReader()
            reader.connect()

        old_user = reader.user_by_employee_id(emp)
        if not old_user:
            return {"ok": False, "skipped": True, "reason": "legacy_user_not_found", "employee_id": emp}

        old_uid = int(old_user["id"])
        wallet = reader.wallet_for_user(old_uid)
        credits, debits = reader.user_ledger_totals(old_uid)
        balance = (
            Decimal(str(wallet["balance"])).quantize(Decimal("0.01"))
            if wallet and wallet.get("balance") is not None
            else (credits - debits).quantize(Decimal("0.01"))
        )

        row = MappingRow(
            old_user_id=old_uid,
            employee_id=emp,
            old_name=str(old_user.get("name") or ""),
            old_email=str(old_user.get("email") or ""),
            channel_i_employee_id=emp,
            channel_i_name=getattr(user, "name", "") or "",
            channel_i_email=getattr(user, "email", "") or "",
            new_user_id=user.pk,
            mapping_status=LegacyWalletMappingStatus.VALID,
        )
        batch = f"{BATCH_PREFIX}:{user.pk}"
        mapping = upsert_mapping(
            row,
            wallet_id=wallet["id"] if wallet else None,
            old_credits=credits,
            old_debits=debits,
            old_balance=balance,
            batch=batch,
        )

        imported = 0
        duplicates = 0
        other = 0
        for txn in reader.iter_wallet_transactions_for_user(old_uid):
            result = import_transaction(txn, old_user, batch)
            if result == "imported":
                imported += 1
            elif result == "duplicate":
                duplicates += 1
            else:
                other += 1

        if mapping.mapping_status != LegacyWalletMappingStatus.IMPORTED and imported:
            mapping.mapping_status = LegacyWalletMappingStatus.IMPORTED
            mapping.save(update_fields=["mapping_status", "updated_at"])

        try:
            dept = get_iic_department()
        except OpeningBalanceError as exc:
            logger.warning(
                "Faculty wallet sync: IIC department missing for user_id=%s: %s",
                user.pk,
                exc,
            )
            return {
                "ok": False,
                "skipped": False,
                "reason": "iic_department_missing",
                "employee_id": emp,
                "imported": imported,
                "duplicates": duplicates,
                "other": other,
                "legacy_balance": str(balance),
            }

        migration_id = faculty_login_migration_id(user_id=user.pk, employee_id=emp)
        recon = reconcile_legacy_balance_to_subwallet(
            user=user,
            target_balance=balance,
            migration_id=migration_id,
            department=dept,
            legacy_closing_balance=balance,
            reconciliation_reference=f"login-sync emp={emp}",
        )

        return {
            "ok": True,
            "skipped": False,
            "employee_id": emp,
            "legacy_user_id": old_uid,
            "legacy_balance": str(balance),
            "imported": imported,
            "duplicates": duplicates,
            "other": other,
            "reconcile": {
                "delta": str(recon.delta),
                "target": str(recon.target_balance),
                "previous_credited": str(recon.previous_credited),
                "created": recon.created,
            },
        }
    except OldMySQLNotConfigured:
        return {"ok": False, "skipped": True, "reason": "legacy_mysql_not_configured"}
    except Exception:
        logger.exception("Faculty login wallet sync failed for user_id=%s", getattr(user, "pk", None))
        return {"ok": False, "skipped": False, "reason": "error"}
    finally:
        if owns_reader and reader is not None:
            try:
                reader.close()
            except Exception:  # noqa: BLE001
                pass


def maybe_sync_faculty_wallet_on_login(user) -> None:
    """Fire-and-forget wrapper for auth views — never raises."""
    try:
        result = sync_faculty_wallet_from_legacy(user)
        if result.get("ok"):
            logger.info(
                "Faculty wallet sync ok user_id=%s emp=%s imported=%s delta=%s",
                getattr(user, "pk", None),
                result.get("employee_id"),
                result.get("imported"),
                (result.get("reconcile") or {}).get("delta"),
            )
        elif not result.get("skipped"):
            logger.warning(
                "Faculty wallet sync incomplete user_id=%s reason=%s",
                getattr(user, "pk", None),
                result.get("reason"),
            )
    except Exception:
        logger.exception(
            "Faculty login wallet sync wrapper failed for user_id=%s",
            getattr(user, "pk", None),
        )
