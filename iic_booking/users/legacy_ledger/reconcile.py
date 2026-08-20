"""Ledger-authoritative reconciliation. Balance columns are comparison only."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Count, Q, Sum

from iic_booking.users.models.portal_migration import (
    LegacyLedgerDirection,
    LegacyWalletAccountMapping,
    LegacyWalletLedgerEntry,
    LegacyWalletMappingStatus,
    LegacyWalletSyncDeadLetter,
)


def _money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def reconcile_employee(mapping: LegacyWalletAccountMapping) -> dict:
    ledger = LegacyWalletLedgerEntry.objects.filter(mapping=mapping)
    imported_count = ledger.count()
    imported_credits = _money(ledger.filter(direction=LegacyLedgerDirection.CREDIT).aggregate(s=Sum("amount"))["s"])
    imported_debits = _money(ledger.filter(direction=LegacyLedgerDirection.DEBIT).aggregate(s=Sum("amount"))["s"])
    old_credits = _money(mapping.old_credits)
    old_debits = _money(mapping.old_debits)
    old_closing = old_credits - old_debits
    imported_closing = imported_credits - imported_debits
    difference = imported_closing - old_closing
    exception_statuses = {
        LegacyWalletMappingStatus.MISSING_EMPLOYEE_ID,
        LegacyWalletMappingStatus.DUPLICATE_EMPLOYEE_ID,
        LegacyWalletMappingStatus.CHANNEL_I_NOT_FOUND,
        LegacyWalletMappingStatus.MULTIPLE_MATCH,
        LegacyWalletMappingStatus.MANUAL_REVIEW,
        LegacyWalletMappingStatus.EXCEPTION,
    }
    if mapping.mapping_status in exception_statuses:
        status = "EXCEPTION"
    elif difference == 0 and imported_credits == old_credits and imported_debits == old_debits:
        status = "PASS"
    else:
        status = "FAIL"
    mapping.imported_credits = imported_credits
    mapping.imported_debits = imported_debits
    mapping.reconciliation_status = status
    if status == "PASS" and mapping.mapping_status in (
        LegacyWalletMappingStatus.VALID,
        LegacyWalletMappingStatus.MAPPED,
        LegacyWalletMappingStatus.IMPORTED,
        LegacyWalletMappingStatus.RECONCILED,
        LegacyWalletMappingStatus.MISMATCH,
    ):
        mapping.mapping_status = LegacyWalletMappingStatus.RECONCILED
    elif status == "FAIL":
        mapping.mapping_status = LegacyWalletMappingStatus.MISMATCH
    mapping.save(
        update_fields=[
            "imported_credits",
            "imported_debits",
            "reconciliation_status",
            "mapping_status",
            "updated_at",
        ]
    )
    return {
        "employee_id": mapping.employee_id,
        "old_user_id": mapping.old_user_id,
        "new_user_id": mapping.new_user_id,
        "mapping_status": mapping.mapping_status,
        "old_transaction_count": None,
        "imported_transaction_count": imported_count,
        "old_credit_total": str(old_credits),
        "imported_credit_total": str(imported_credits),
        "old_debit_total": str(old_debits),
        "imported_debit_total": str(imported_debits),
        "old_closing_balance": str(old_closing),
        "imported_closing_balance": str(imported_closing),
        "old_wallet_balance_column": str(mapping.old_wallet_balance) if mapping.old_wallet_balance is not None else None,
        "difference": str(difference),
        "status": status,
        "recommended_action": _action(status, mapping),
    }


def _action(status: str, mapping: LegacyWalletAccountMapping) -> str:
    if status == "PASS":
        return "No action."
    if status == "EXCEPTION":
        return (
            f"Do not import automatically. Review Employee ID {mapping.employee_id}: "
            f"{mapping.exception_reason or mapping.mapping_status}"
        )
    return "Do not cut over. Compare old wallet_transactions for this user_id with imported ledger rows."


def run_full_reconciliation() -> dict:
    rows = []
    counts = {"PASS": 0, "FAIL": 0, "EXCEPTION": 0}
    for mapping in LegacyWalletAccountMapping.objects.all().order_by("employee_id"):
        rec = reconcile_employee(mapping)
        rows.append(rec)
        counts[rec["status"]] += 1
    overall = "PASS"
    if counts["FAIL"]:
        overall = "FAIL"
    elif counts["EXCEPTION"]:
        overall = "EXCEPTION"
    if not rows:
        overall = "EXCEPTION"
    totals = LegacyWalletLedgerEntry.objects.aggregate(
        credits=Sum("amount", filter=Q(direction=LegacyLedgerDirection.CREDIT)),
        debits=Sum("amount", filter=Q(direction=LegacyLedgerDirection.DEBIT)),
        n=Count("id"),
    )
    return {
        "overall_status": overall,
        "counts": counts,
        "ledger_rows": totals["n"] or 0,
        "imported_credit_total": str(_money(totals["credits"])),
        "imported_debit_total": str(_money(totals["debits"])),
        "old_credit_total": str(_money(sum((_money(r["old_credit_total"]) for r in rows), Decimal("0")))),
        "old_debit_total": str(_money(sum((_money(r["old_debit_total"]) for r in rows), Decimal("0")))),
        "dead_letters": LegacyWalletSyncDeadLetter.objects.count(),
        "zero_unresolved_mismatches": counts["FAIL"] == 0,
        "rows": rows,
    }
