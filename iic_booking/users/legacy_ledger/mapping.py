"""Exact Employee-ID mapping. Email/name are secondary validation only."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, asdict
from decimal import Decimal

from iic_booking.users.legacy_ledger.channel_i_identity import is_wallet_migration_eligible
from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import (
    LegacyWalletAccountMapping,
    LegacyWalletMappingStatus,
)


def exact_employee_id(raw) -> str:
    if raw is None:
        return ""
    return str(raw).strip()


def load_new_users_by_employee_id() -> dict[str, list]:
    by: dict[str, list] = defaultdict(list)
    qs = User.objects.exclude(emp_id__isnull=True).exclude(emp_id="")
    for u in qs.only("id", "emp_id", "name", "email", "is_active"):
        emp = exact_employee_id(u.emp_id)
        if emp:
            by[emp].append(u)
    return by


@dataclass
class MappingRow:
    old_user_id: int
    employee_id: str
    old_name: str
    old_email: str
    channel_i_employee_id: str
    channel_i_name: str
    channel_i_email: str
    new_user_id: int | None
    mapping_status: str
    exception_reason: str = ""


def classify_old_user(
    user_row: dict,
    duplicate_ids: set[str],
    new_users_by_emp: dict[str, list] | None = None,
    *,
    require_verified_identity: bool = True,
    identity_source: str = "LEGACY_UNVERIFIED",
) -> MappingRow:
    old_id = int(user_row["id"])
    emp = exact_employee_id(user_row.get("emp_id"))
    old_name = str(user_row.get("name") or "")
    old_email = str(user_row.get("email") or "")
    if not emp:
        return MappingRow(
            old_user_id=old_id,
            employee_id="",
            old_name=old_name,
            old_email=old_email,
            channel_i_employee_id="",
            channel_i_name="",
            channel_i_email="",
            new_user_id=None,
            mapping_status=LegacyWalletMappingStatus.MISSING_EMPLOYEE_ID,
            exception_reason="WALLET_MAPPING_EXCEPTION: employee ID missing",
        )
    if emp in duplicate_ids:
        return MappingRow(
            old_user_id=old_id,
            employee_id=emp,
            old_name=old_name,
            old_email=old_email,
            channel_i_employee_id="",
            channel_i_name="",
            channel_i_email="",
            new_user_id=None,
            mapping_status=LegacyWalletMappingStatus.DUPLICATE_EMPLOYEE_ID,
            exception_reason="WALLET_MAPPING_EXCEPTION: duplicate employee ID on old portal",
        )
    if new_users_by_emp is None:
        matches = list(User.objects.filter(emp_id=emp))
    else:
        matches = new_users_by_emp.get(emp, [])
    if not matches:
        return MappingRow(
            old_user_id=old_id,
            employee_id=emp,
            old_name=old_name,
            old_email=old_email,
            channel_i_employee_id="",
            channel_i_name="",
            channel_i_email="",
            new_user_id=None,
            mapping_status=LegacyWalletMappingStatus.CHANNEL_I_NOT_FOUND,
            exception_reason="WALLET_MAPPING_EXCEPTION: no new-portal user with this employee ID",
        )
    if len(matches) > 1:
        return MappingRow(
            old_user_id=old_id,
            employee_id=emp,
            old_name=old_name,
            old_email=old_email,
            channel_i_employee_id=emp,
            channel_i_name="",
            channel_i_email="",
            new_user_id=None,
            mapping_status=LegacyWalletMappingStatus.MULTIPLE_MATCH,
            exception_reason="WALLET_MAPPING_EXCEPTION: multiple new-portal users share this employee ID",
        )
    nu = matches[0]
    source = identity_source if require_verified_identity else "CHANNEL_I_VERIFIED"
    eligible, reason = is_wallet_migration_eligible(
        employee_id=emp,
        production_user_count=1,
        identity_source=source,
        has_conflict=False,
        user_is_active=bool(getattr(nu, "is_active", True)),
    )
    if not eligible:
        return MappingRow(
            old_user_id=old_id,
            employee_id=emp,
            old_name=old_name,
            old_email=old_email,
            channel_i_employee_id=exact_employee_id(nu.emp_id),
            channel_i_name=nu.name or "",
            channel_i_email=nu.email or "",
            new_user_id=None,
            mapping_status=LegacyWalletMappingStatus.EXCEPTION,
            exception_reason=reason,
        )
    return MappingRow(
        old_user_id=old_id,
        employee_id=emp,
        old_name=old_name,
        old_email=old_email,
        channel_i_employee_id=exact_employee_id(nu.emp_id),
        channel_i_name=nu.name or "",
        channel_i_email=nu.email or "",
        new_user_id=nu.pk,
        mapping_status=LegacyWalletMappingStatus.VALID,
    )


def upsert_mapping(row: MappingRow, wallet_id=None, old_credits=None, old_debits=None, old_balance=None, batch="") -> LegacyWalletAccountMapping:
    defaults = {
        "old_user_id": row.old_user_id,
        "old_wallet_id": wallet_id,
        "old_name": row.old_name,
        "old_email": row.old_email,
        "new_user_id": row.new_user_id,
        "channel_i_employee_id": row.channel_i_employee_id,
        "channel_i_email": row.channel_i_email,
        "channel_i_name": row.channel_i_name,
        "mapping_status": row.mapping_status,
        "exception_reason": row.exception_reason,
        "migration_batch": batch,
    }
    if old_credits is not None:
        defaults["old_credits"] = old_credits
    if old_debits is not None:
        defaults["old_debits"] = old_debits
    if old_balance is not None:
        defaults["old_wallet_balance"] = old_balance
    if not row.employee_id:
        obj, _ = LegacyWalletAccountMapping.objects.update_or_create(
            employee_id=f"MISSING:{row.old_user_id}",
            defaults=defaults,
        )
        return obj
    obj, _ = LegacyWalletAccountMapping.objects.update_or_create(
        employee_id=row.employee_id,
        defaults=defaults,
    )
    return obj


def mapping_report_dict(row: MappingRow) -> dict:
    return asdict(row)


def imported_totals_match(old_credits: Decimal, old_debits: Decimal, imported_credits: Decimal, imported_debits: Decimal) -> bool:
    return (old_credits - old_debits) == (imported_credits - imported_debits)
