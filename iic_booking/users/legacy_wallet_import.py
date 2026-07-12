"""
Import opening wallet balances from legacy MySQL export (admin.users + user_wallet.balance by emp_id).

Expected CSV columns (header row, case-insensitive):
  emp_id (required), balance (required), email (optional)
"""

from __future__ import annotations

import csv
import logging
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.db import transaction

from .models import Department, DepartmentType, SubWallet, SubWalletTransaction, User, Wallet
from .repositories.wallet_repository import SubWalletRepository

logger = logging.getLogger(__name__)

MIGRATION_DESC_PREFIX = "Legacy migration opening balance"


def normalize_emp_id(raw: str) -> str:
    """Strip and normalize emp_id (digits only, legacy often uses 6-digit ids)."""
    s = (raw or "").strip()
    if not s:
        return ""
    digits = re.sub(r"\D", "", s)
    if digits:
        return digits.lstrip("0") or "0"
    return s


def _parse_balance(raw: str) -> Optional[Decimal]:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "")
    if not s:
        return None
    try:
        amount = Decimal(s)
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return amount.quantize(Decimal("0.01"))


def _parse_legacy_wallet_dict_reader(reader: csv.DictReader) -> List[Dict[str, str]]:
    if not reader.fieldnames:
        raise ValueError("CSV file is empty or has no header row.")
    normalized = {(h or "").strip().lower(): h for h in reader.fieldnames if h}
    emp_key = normalized.get("emp_id") or normalized.get("empid") or normalized.get("emp no")
    bal_key = normalized.get("balance") or normalized.get("legacy_balance") or normalized.get("amount")
    if not emp_key or not bal_key:
        raise ValueError(
            "CSV must include emp_id and balance columns. "
            f"Found headers: {list(reader.fieldnames)}"
        )
    email_key = normalized.get("email")
    rows: List[Dict[str, str]] = []
    for line in reader:
        emp = normalize_emp_id(line.get(emp_key) or "")
        bal = (line.get(bal_key) or "").strip()
        if not emp:
            continue
        row: Dict[str, str] = {"emp_id": emp, "balance": bal}
        if email_key:
            row["email"] = (line.get(email_key) or "").strip()
        rows.append(row)
    return rows


def read_legacy_wallet_csv_from_text(content: str) -> List[Dict[str, str]]:
    import io

    with io.StringIO(content) as f:
        return _parse_legacy_wallet_dict_reader(csv.DictReader(f))


def read_legacy_wallet_csv(path: str) -> List[Dict[str, str]]:
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        return _parse_legacy_wallet_dict_reader(csv.DictReader(f))


def resolve_migration_department(
    user: User,
    *,
    department_id: Optional[int] = None,
    use_general: bool = False,
) -> Optional[Department]:
    if department_id is not None:
        try:
            return Department.objects.get(pk=department_id, department_type=DepartmentType.INTERNAL)
        except Department.DoesNotExist:
            return None
    if use_general:
        dept, _ = Department.objects.get_or_create(
            name="General",
            defaults={
                "department_type": DepartmentType.INTERNAL,
                "code": "GENERAL",
                "description": "Default department for equipment without assignment",
            },
        )
        return dept
    if user.department_id and getattr(user.department, "department_type", None) == DepartmentType.INTERNAL:
        return user.department
    dept, _ = Department.objects.get_or_create(
        name="General",
        defaults={
            "department_type": DepartmentType.INTERNAL,
            "code": "GENERAL",
            "description": "Default department for equipment without assignment",
        },
    )
    return dept


def _migration_already_applied(sub_wallet: SubWallet, batch_id: str) -> bool:
    needle = f"({batch_id})"
    return SubWalletTransaction.objects.filter(
        sub_wallet=sub_wallet,
        transaction_type=SubWalletTransaction.TransactionType.CREDIT,
        description__contains=needle,
    ).exists()


def _find_user_by_emp_id(emp_id: str) -> Optional[User]:
    normalized = normalize_emp_id(emp_id)
    if not normalized:
        return None
    candidates = User.objects.filter(emp_id__isnull=False).exclude(emp_id="")
    for user in candidates:
        if normalize_emp_id(user.emp_id or "") == normalized:
            return user
    return None


def _find_user_for_legacy_row(row: Dict[str, str]) -> Optional[User]:
    user = _find_user_by_emp_id(row.get("emp_id") or "")
    if user:
        return user
    email_hint = row.get("email") or ""
    if email_hint:
        return User.objects.filter(email__iexact=email_hint).first()
    return None


def lookup_legacy_wallet_for_emp_id(
    emp_id: str,
    *,
    batch_id: Optional[str] = None,
    department_id: Optional[int] = None,
    use_general_department: bool = False,
    mysql_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Fetch balance from legacy MySQL and match to a new-system user."""
    from .legacy_wallet_db import (
        LegacyWalletConnectionError,
        LegacyWalletNotConfigured,
        fetch_legacy_wallet_by_emp_id,
        is_legacy_mysql_configured,
        resolve_legacy_mysql_config,
    )

    normalized_input = normalize_emp_id(emp_id)
    configured = is_legacy_mysql_configured(mysql_config)
    result: Dict[str, Any] = {
        "emp_id_input": (emp_id or "").strip(),
        "emp_id_normalized": normalized_input,
        "legacy_mysql_configured": configured,
        "connection_source": "request" if mysql_config else "environment",
        "legacy": None,
        "new_system": None,
        "status": "not_found",
    }

    if not normalized_input:
        result["status"] = "invalid_emp_id"
        return result

    try:
        legacy_row = fetch_legacy_wallet_by_emp_id(emp_id, mysql_config=mysql_config)
        if configured:
            cfg = resolve_legacy_mysql_config(mysql_config)
            result["legacy_mysql_host"] = cfg["host"]
            result["legacy_mysql_database"] = cfg["database"]
    except LegacyWalletNotConfigured as e:
        result["status"] = "not_configured"
        result["error"] = str(e)
        return result
    except LegacyWalletConnectionError as e:
        result["status"] = "connection_error"
        result["error"] = str(e)
        return result

    if not legacy_row:
        result["status"] = "not_found"
        return result

    result["legacy"] = legacy_row
    amount = _parse_balance(legacy_row.get("balance"))
    if amount is None:
        result["status"] = "invalid_balance"
        return result
    if amount == 0:
        result["status"] = "zero_balance"
        return result

    user = _find_user_for_legacy_row(
        {"emp_id": legacy_row.get("emp_id") or normalized_input, "email": legacy_row.get("email") or ""}
    )
    if not user:
        result["status"] = "unmatched_new_system"
        return result

    department = resolve_migration_department(
        user,
        department_id=department_id,
        use_general=use_general_department,
    )
    dept_label = (department.code or department.name) if department else None
    result["new_system"] = {
        "user_id": user.id,
        "user_email": user.email,
        "user_name": user.name or user.email,
        "emp_id": user.emp_id,
        "department": dept_label,
    }

    status = "ready"
    if batch_id and department:
        wallet, _ = Wallet.objects.get_or_create(user=user)
        sub_wallet = SubWalletRepository.get_or_create(wallet, department)
        if _migration_already_applied(sub_wallet, batch_id):
            status = "already_imported"
    result["status"] = status
    return result


def preview_legacy_wallet_balances(
    rows: List[Dict[str, str]],
    *,
    batch_id: Optional[str] = None,
    department_id: Optional[int] = None,
    use_general_department: bool = False,
) -> Dict[str, Any]:
    """Match legacy CSV rows to new-system users without crediting."""
    preview: List[Dict[str, Any]] = []
    total_legacy = Decimal("0.00")
    matched_count = 0
    unmatched_count = 0
    ready_count = 0

    for row in rows:
        emp_id = row.get("emp_id") or ""
        amount = _parse_balance(row.get("balance"))
        legacy_email = row.get("email") or ""

        if amount is None:
            preview.append(
                {
                    "emp_id": emp_id,
                    "legacy_email": legacy_email,
                    "balance": row.get("balance"),
                    "status": "invalid_balance",
                    "matched": False,
                }
            )
            unmatched_count += 1
            continue

        if amount == 0:
            preview.append(
                {
                    "emp_id": emp_id,
                    "legacy_email": legacy_email,
                    "balance": "0.00",
                    "status": "zero_balance",
                    "matched": False,
                }
            )
            continue

        total_legacy += amount
        user = _find_user_for_legacy_row(row)
        if not user:
            preview.append(
                {
                    "emp_id": emp_id,
                    "legacy_email": legacy_email,
                    "balance": str(amount),
                    "status": "unmatched",
                    "matched": False,
                }
            )
            unmatched_count += 1
            continue

        matched_count += 1
        department = resolve_migration_department(
            user,
            department_id=department_id,
            use_general=use_general_department,
        )
        dept_label = (department.code or department.name) if department else None
        status = "ready"
        if batch_id and department:
            wallet, _ = Wallet.objects.get_or_create(user=user)
            sub_wallet = SubWalletRepository.get_or_create(wallet, department)
            if _migration_already_applied(sub_wallet, batch_id):
                status = "already_imported"

        if status == "ready":
            ready_count += 1

        preview.append(
            {
                "emp_id": emp_id,
                "legacy_email": legacy_email,
                "balance": str(amount),
                "status": status,
                "matched": True,
                "user_id": user.id,
                "user_email": user.email,
                "user_name": user.name or user.email,
                "department": dept_label,
            }
        )

    return {
        "row_count": len(preview),
        "matched_count": matched_count,
        "unmatched_count": unmatched_count,
        "ready_count": ready_count,
        "total_legacy_balance": str(total_legacy.quantize(Decimal("0.01"))),
        "rows": preview,
    }


def import_legacy_wallet_balances(
    rows: List[Dict[str, str]],
    *,
    batch_id: str,
    department_id: Optional[int] = None,
    use_general_department: bool = False,
    dry_run: bool = False,
    skip_zero: bool = True,
) -> Dict[str, Any]:
    """
    Credit legacy balances into sub-wallets on the new system.

    Idempotent per batch_id: skips users already credited with the same batch tag.
    """
    credited = 0
    skipped = 0
    errors: List[str] = []
    total_amount = Decimal("0.00")
    processed: List[Dict[str, Any]] = []

    for row in rows:
        emp_id = row.get("emp_id") or ""
        amount = _parse_balance(row.get("balance"))
        if amount is None:
            errors.append(f"emp_id {emp_id}: invalid balance {row.get('balance')!r}")
            skipped += 1
            continue
        if skip_zero and amount == 0:
            skipped += 1
            continue

        user = _find_user_for_legacy_row(row)
        if not user:
            errors.append(f"emp_id {emp_id}: no matching user on new system")
            skipped += 1
            continue

        department = resolve_migration_department(
            user,
            department_id=department_id,
            use_general=use_general_department,
        )
        if not department:
            errors.append(f"emp_id {emp_id}: could not resolve internal department")
            skipped += 1
            continue

        wallet, _ = Wallet.objects.get_or_create(user=user)
        sub_wallet = SubWalletRepository.get_or_create(wallet, department)

        if _migration_already_applied(sub_wallet, batch_id):
            skipped += 1
            continue

        description = f"{MIGRATION_DESC_PREFIX} ({batch_id}) — emp_id {emp_id}"

        if dry_run:
            credited += 1
            total_amount += amount
            processed.append(
                {
                    "emp_id": emp_id,
                    "email": user.email,
                    "amount": str(amount),
                    "department": department.code or department.name,
                    "dry_run": True,
                }
            )
            continue

        with transaction.atomic():
            sub_wallet.credit(amount, description, related_user=user)
            credited += 1
            total_amount += amount
            processed.append(
                {
                    "emp_id": emp_id,
                    "email": user.email,
                    "amount": str(amount),
                    "department": department.code or department.name,
                }
            )
            logger.info(
                "Legacy wallet import %s: %s emp_id=%s ₹%s → %s",
                batch_id,
                user.email,
                emp_id,
                amount,
                department.code or department.name,
            )

    return {
        "batch_id": batch_id,
        "credited": credited,
        "skipped": skipped,
        "errors": errors,
        "total_amount": str(total_amount.quantize(Decimal("0.01"))),
        "processed": processed,
    }
