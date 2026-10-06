"""One-time migration opening-balance helper (staging-safe, idempotent).

Creates a SubWalletTransaction description tagged with migration_id.
Second call for the same migration_id is rejected (no duplicate).

Also provides reconcile_legacy_balance_to_subwallet for faculty login sync,
which credits/debits deltas into a target department SubWallet using markers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from iic_booking.users.models import Department, DepartmentType, SubWallet, SubWalletTransaction, Wallet
from iic_booking.users.models.portal_migration import LegacyLedgerDirection, LegacyWalletLedgerEntry

OPENING_SOURCE = "LEGACY_PORTAL_MIGRATION"
DESC_PREFIX = "Legacy migration opening balance"
RECONCILE_PREFIX = "Legacy faculty wallet sync"
ADMIN_SYNC_PREFIX = "Legacy wallet sync (admin)"
LEGACY_CREDIT_PREFIXES = (RECONCILE_PREFIX, DESC_PREFIX, ADMIN_SYNC_PREFIX)
IIC_DEPARTMENT_NAME = "Institute Instrumentation Centre"


class OpeningBalanceError(Exception):
    pass


def opening_balance_description(migration_id: str) -> str:
    return f"{DESC_PREFIX} | migration_id={migration_id} | source={OPENING_SOURCE}"


def reconcile_description(migration_id: str, prefix: str = RECONCILE_PREFIX) -> str:
    return f"{prefix} | migration_id={migration_id} | source={OPENING_SOURCE}"


def _general_department() -> Department:
    dept, _ = Department.objects.get_or_create(
        name="General",
        department_type=DepartmentType.INTERNAL,
        defaults={},
    )
    return dept


def get_iic_department() -> Department:
    """Lookup Institute Instrumentation Centre by exact name. Does not create."""
    dept = Department.objects.filter(
        name=IIC_DEPARTMENT_NAME,
        department_type=DepartmentType.INTERNAL,
    ).first()
    if dept is None:
        dept = Department.objects.filter(name=IIC_DEPARTMENT_NAME).first()
    if dept is None:
        raise OpeningBalanceError(
            f'Department "{IIC_DEPARTMENT_NAME}" not found. '
            "Create it before faculty wallet sync can credit SubWallets."
        )
    return dept


def create_migration_opening_balance(
    *,
    user,
    amount: Decimal,
    migration_id: str,
    legacy_closing_balance: Decimal | None = None,
    reconciliation_reference: str = "",
    department: Department | None = None,
) -> SubWalletTransaction:
    migration_id = (migration_id or "").strip()
    if not migration_id:
        raise OpeningBalanceError("migration_id is required")
    amount = Decimal(str(amount)).quantize(Decimal("0.01"))
    if amount < 0:
        raise OpeningBalanceError("opening balance cannot be negative")

    marker = f"migration_id={migration_id}"
    existing = SubWalletTransaction.objects.filter(
        description__contains=marker,
        description__startswith=DESC_PREFIX,
    ).first()
    if existing:
        raise OpeningBalanceError(
            f"Opening balance already exists for {migration_id} (txn={existing.pk})"
        )

    wallet, _ = Wallet.objects.get_or_create(user=user)
    dept = department or _general_department()
    sub, _ = SubWallet.objects.get_or_create(
        wallet=wallet,
        department=dept,
        defaults={"balance": Decimal("0.00")},
    )
    closing = (
        Decimal(str(legacy_closing_balance)).quantize(Decimal("0.01"))
        if legacy_closing_balance is not None
        else amount
    )
    desc = (
        f"{opening_balance_description(migration_id)} | "
        f"legacy_closing={closing} | "
        f"recon={reconciliation_reference or 'n/a'} | "
        f"ts={timezone.now().isoformat()}"
    )
    with transaction.atomic():
        if SubWalletTransaction.objects.filter(
            description__contains=marker,
            description__startswith=DESC_PREFIX,
        ).exists():
            raise OpeningBalanceError(f"Opening balance already exists for {migration_id}")
        sub.balance = (sub.balance or Decimal("0.00")) + amount
        sub.save(update_fields=["balance"])
        txn = SubWalletTransaction.objects.create(
            sub_wallet=sub,
            amount=amount,
            transaction_type=SubWalletTransaction.TransactionType.CREDIT,
            description=desc,
        )
    return txn


_DELTA_RE = re.compile(r"\|\s*delta=(-?\d+(?:\.\d+)?)")
_CLOSING_RE = re.compile(r"\|\s*legacy_closing=(-?\d+(?:\.\d+)?)")
_EMP_MIGRATION_RE = re.compile(r"migration_id=faculty-wallet:([^\s|]+)\s*\|")
# Ledger rows of one sync are imported just before its reconcile transaction is written.
_SYNC_IMPORT_WINDOW = timedelta(minutes=5)


def _inr(value: Decimal) -> str:
    return f"₹{value.quantize(Decimal('0.01')):,.2f}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _same_sync_ledger_rows(txn: SubWalletTransaction, delta: Decimal) -> list | None:
    """Old-portal ledger rows imported by the same sync run, when they account for the whole delta."""
    m = _EMP_MIGRATION_RE.search(txn.description or "")
    if not m or not txn.created_at:
        return None
    previous = (
        SubWalletTransaction.objects.filter(
            sub_wallet_id=txn.sub_wallet_id,
            description__contains=f"migration_id=faculty-wallet:{m.group(1)} |",
            id__lt=txn.id,
        )
        .order_by("-id")
        .values_list("created_at", flat=True)
        .first()
    )
    since = txn.created_at - _SYNC_IMPORT_WINDOW
    if previous is not None and previous > since:
        since = previous
    rows = list(
        LegacyWalletLedgerEntry.objects.filter(
            employee_id=m.group(1),
            imported_at__gt=since,
            imported_at__lte=txn.created_at,
        ).order_by("occurred_at", "source_transaction_id")
    )
    net = sum(
        (r.amount if r.direction == LegacyLedgerDirection.CREDIT else -r.amount for r in rows),
        Decimal("0.00"),
    )
    return rows if rows and net == delta else None


def legacy_sync_display(txn: SubWalletTransaction) -> str | None:
    """Plain-language description of a legacy-portal balance sync row; None for any other transaction.

    The stored description keeps its machine markers (migration_id, delta) because idempotency relies on them.
    """
    desc = (txn.description or "").strip()
    if not desc.startswith(LEGACY_CREDIT_PREFIXES):
        return None
    amount = Decimal(str(txn.amount or 0))
    if desc.startswith(DESC_PREFIX):
        return f"Opening balance carried over from the old IIC portal: {_inr(amount)}"

    by_admin = " (synced by IIC)" if desc.startswith(ADMIN_SYNC_PREFIX) else ""
    is_debit = txn.transaction_type == SubWalletTransaction.TransactionType.DEBIT
    dm = _DELTA_RE.search(desc)
    cm = _CLOSING_RE.search(desc)
    delta = Decimal(dm.group(1)) if dm else (-amount if is_debit else amount)
    closing = Decimal(cm.group(1)) if cm else None
    closing_text = f" Old portal balance now {_inr(closing)}." if closing is not None else ""

    if not is_debit and closing is not None and delta == closing:
        return f"Balance carried over from the old IIC portal: {_inr(amount)}{by_admin}"

    rows = _same_sync_ledger_rows(txn, delta)
    if rows:
        charges = [r for r in rows if r.direction == LegacyLedgerDirection.DEBIT]
        credits = [r for r in rows if r.direction == LegacyLedgerDirection.CREDIT]
        if charges and credits:
            parts = [
                f"{_plural(len(charges), 'charge')} of {_inr(sum((r.amount for r in charges), Decimal('0')))}",
                f"{_plural(len(credits), 'credit')} of {_inr(sum((r.amount for r in credits), Decimal('0')))}",
            ]
        else:
            parts = [_plural(len(charges), "charge") if charges else _plural(len(credits), "credit")]
        first = timezone.localtime(rows[0].occurred_at).strftime("%d %b %Y")
        last = timezone.localtime(rows[-1].occurred_at).strftime("%d %b %Y")
        when = f"on {first}" if first == last else f"between {first} and {last}"
        verb = "deducted" if is_debit else "added"
        return (
            f"Old IIC portal balance update{by_admin}: {_inr(amount)} {verb} for "
            f"{' and '.join(parts)} made on the old portal {when}, after its balance was last copied here."
            f"{closing_text}"
        )

    change = "fell" if is_debit else "rose"
    return (
        f"Old IIC portal balance update{by_admin}: the old portal balance {change} by {_inr(amount)} "
        f"since it was last copied here.{closing_text}"
    )


@dataclass
class ReconcileResult:
    sub_wallet: SubWallet
    transaction: SubWalletTransaction | None
    delta: Decimal
    target_balance: Decimal
    previous_credited: Decimal
    created: bool


def net_credited_for_migration(*, migration_id: str, sub_wallet: SubWallet | None = None) -> Decimal:
    """Net legacy-migration credit for migration_id (one sub-wallet, or all when sub_wallet is None)."""
    marker = f"migration_id={migration_id} |"
    qs = SubWalletTransaction.objects.filter(description__contains=marker)
    if sub_wallet is not None:
        qs = qs.filter(sub_wallet=sub_wallet)
    total = Decimal("0.00")
    for txn in qs:
        desc = txn.description or ""
        if not desc.startswith(LEGACY_CREDIT_PREFIXES):
            continue
        amt = Decimal(str(txn.amount or 0)).quantize(Decimal("0.01"))
        if txn.transaction_type == SubWalletTransaction.TransactionType.CREDIT:
            total += amt
        elif txn.transaction_type == SubWalletTransaction.TransactionType.DEBIT:
            total -= amt
    return total.quantize(Decimal("0.01"))


def reconcile_legacy_balance_to_subwallet(
    *,
    user,
    target_balance: Decimal,
    migration_id: str,
    department: Department | None = None,
    legacy_closing_balance: Decimal | None = None,
    reconciliation_reference: str = "",
    wallet: Wallet | None = None,
    related_user=None,
    description_prefix: str = RECONCILE_PREFIX,
) -> ReconcileResult:
    """
    Bring the SubWallet (default: user's own wallet, IIC department) net migration credit
    in line with target_balance using marker-based credit/debit deltas.

    Pass ``wallet`` to credit a wallet the user does not own (IITR Student → linked faculty wallet).
    Idempotent: a second call with the same target creates no new transaction.
    """
    migration_id = (migration_id or "").strip()
    if not migration_id:
        raise OpeningBalanceError("migration_id is required")
    target = Decimal(str(target_balance)).quantize(Decimal("0.01"))
    if target < 0:
        raise OpeningBalanceError("target balance cannot be negative")

    dept = department or get_iic_department()
    if wallet is None:
        wallet, _ = Wallet.objects.get_or_create(user=user)
    sub, _ = SubWallet.objects.get_or_create(
        wallet=wallet,
        department=dept,
        defaults={"balance": Decimal("0.00")},
    )
    closing = (
        Decimal(str(legacy_closing_balance)).quantize(Decimal("0.01"))
        if legacy_closing_balance is not None
        else target
    )
    with transaction.atomic():
        sub = SubWallet.objects.select_for_update().get(pk=sub.pk)
        # Checked under lock for concurrent logins / admin syncs.
        previous = net_credited_for_migration(migration_id=migration_id, sub_wallet=sub)
        delta = (target - previous).quantize(Decimal("0.01"))
        if delta == 0:
            return ReconcileResult(
                sub_wallet=sub,
                transaction=None,
                delta=delta,
                target_balance=target,
                previous_credited=previous,
                created=False,
            )
        txn_type = (
            SubWalletTransaction.TransactionType.CREDIT
            if delta > 0
            else SubWalletTransaction.TransactionType.DEBIT
        )
        abs_delta = abs(delta)
        desc = (
            f"{reconcile_description(migration_id, description_prefix)} | "
            f"delta={delta} | legacy_closing={closing} | "
            f"recon={reconciliation_reference or 'n/a'} | "
            f"ts={timezone.now().isoformat()}"
        )
        if txn_type == SubWalletTransaction.TransactionType.CREDIT:
            sub.balance = (sub.balance or Decimal("0.00")) + abs_delta
        else:
            sub.balance = (sub.balance or Decimal("0.00")) - abs_delta
        sub.save(update_fields=["balance"])
        txn = SubWalletTransaction.objects.create(
            sub_wallet=sub,
            amount=abs_delta,
            transaction_type=txn_type,
            description=desc,
            related_user=related_user,
        )
    return ReconcileResult(
        sub_wallet=sub,
        transaction=txn,
        delta=delta,
        target_balance=target,
        previous_credited=previous,
        created=True,
    )
