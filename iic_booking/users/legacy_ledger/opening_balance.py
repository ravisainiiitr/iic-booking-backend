"""One-time migration opening-balance helper (staging-safe, idempotent).

Creates a SubWalletTransaction description tagged with migration_id.
Second call for the same migration_id is rejected (no duplicate).

Also provides reconcile_legacy_balance_to_subwallet for faculty login sync,
which credits/debits deltas into a target department SubWallet using markers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from iic_booking.users.models import Department, DepartmentType, SubWallet, SubWalletTransaction, Wallet

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
