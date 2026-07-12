"""Repository for Wallet and SubWallet models."""

from typing import Optional, Union

from decimal import Decimal

from django.db.models import QuerySet

from ..models import Wallet, User, SubWallet, SubWalletTransaction
from ..models.department import Department, DepartmentType


def get_internal_departments_with_equipment():
    """Return internal departments that have at least one equipment (for sub-wallet creation)."""
    from iic_booking.equipment.models import Equipment
    ids = set(
        Equipment.objects.filter(internal_department_id__isnull=False).values_list(
            "internal_department_id", flat=True
        )
    )
    # Include General for fallback (equipment without department)
    general = Department.objects.filter(name="General", department_type=DepartmentType.INTERNAL).first()
    if general:
        ids.add(general.id)
    return Department.objects.filter(id__in=ids, department_type=DepartmentType.INTERNAL).order_by("name")


def resolve_internal_department_for_wallet_recharge(
    wallet: Wallet, department_id: int
) -> Optional[Department]:
    """
    Department allowed for wallet recharge / credit preflight if either:
    - It appears in get_internal_departments_with_equipment(), or
    - This wallet already has a sub-wallet for that department and the department is INTERNAL.

    Covers faculty whose balance (e.g. ₹350) sits in a legacy or reorganized department that no
    longer has equipment rows pointing at it — otherwise preflight returns invalid_department and
    the credit popup never appears.
    """
    allowed = get_internal_departments_with_equipment()
    try:
        return allowed.get(pk=department_id)
    except Department.DoesNotExist:
        pass
    try:
        sw = SubWallet.objects.select_related("department").get(
            wallet_id=wallet.id, department_id=department_id
        )
    except SubWallet.DoesNotExist:
        return None
    dept = sw.department
    if dept is None or dept.department_type != DepartmentType.INTERNAL:
        return None
    return dept


def get_departments_for_wallet_recharge(wallet: Optional[Wallet]) -> QuerySet[Department]:
    """Union of equipment-linked internal departments and internal departments that already have a sub-wallet."""
    equipment_ids = list(get_internal_departments_with_equipment().values_list("id", flat=True))
    if not wallet:
        return Department.objects.filter(id__in=equipment_ids, department_type=DepartmentType.INTERNAL).order_by(
            "name"
        )
    sub_dept_ids = SubWallet.objects.filter(wallet=wallet).values_list("department_id", flat=True)
    internal_from_wallet = Department.objects.filter(
        id__in=sub_dept_ids,
        department_type=DepartmentType.INTERNAL,
    ).values_list("id", flat=True)
    merged = sorted(set(equipment_ids) | set(internal_from_wallet))
    return Department.objects.filter(id__in=merged, department_type=DepartmentType.INTERNAL).order_by("name")


class WalletRepository:
    """Repository for Wallet operations."""

    @staticmethod
    def get_by_user(user: User) -> Optional[Wallet]:
        """Get wallet by user."""
        try:
            return Wallet.objects.get(user=user)
        except Wallet.DoesNotExist:
            return None

    @staticmethod
    def get_by_id(pk: int) -> Optional[Wallet]:
        """Get wallet by primary key."""
        try:
            return Wallet.objects.select_related("user").get(pk=pk)
        except Wallet.DoesNotExist:
            return None

    @staticmethod
    def get_all() -> QuerySet[Wallet]:
        """Get all wallets."""
        return Wallet.objects.select_related("user").all()

    @staticmethod
    def create(user: User) -> Wallet:
        """Create a new wallet for a user (sub-wallets hold balance)."""
        return Wallet.objects.create(user=user)

    @staticmethod
    def get_or_create(user: User) -> tuple[Wallet, bool]:
        """Get or create wallet for a user."""
        return Wallet.objects.get_or_create(user=user)

    @staticmethod
    def get_booking_wallet_target(
        user: User, department: Optional[Department]
    ) -> tuple[Optional[Union[Wallet, SubWallet]], bool]:
        """Return the sub-wallet to use for a booking (debit/credit).

        Wallet is consolidated from sub-wallets only. If equipment has no internal_department,
        uses the 'General' internal department sub-wallet. Otherwise uses the sub-wallet for that department.
        """
        from ..models.department import DepartmentType
        wallet = user.get_accessible_wallet()
        if not wallet:
            return None, False
        if not department:
            general, _ = Department.objects.get_or_create(
                name="General",
                defaults={
                    "department_type": DepartmentType.INTERNAL,
                    "code": "GENERAL",
                    "description": "Default department for equipment without assignment",
                },
            )
            department = general
        sub = SubWalletRepository.get_or_create(wallet, department)
        return sub, True


class SubWalletRepository:
    """Repository for SubWallet operations."""

    @staticmethod
    def get_or_create(wallet: Wallet, department: Department) -> SubWallet:
        """Get or create sub-wallet for this wallet and internal department."""
        sub, _ = SubWallet.objects.get_or_create(
            wallet=wallet,
            department=department,
            defaults={"balance": Decimal("0.00")},
        )
        return sub

    @staticmethod
    def get_by_wallet(wallet: Wallet) -> QuerySet[SubWallet]:
        """Get all sub-wallets for a wallet."""
        return SubWallet.objects.filter(wallet=wallet).select_related("department")

    @staticmethod
    def get_by_id(pk: int) -> Optional[SubWallet]:
        """Get sub-wallet by primary key."""
        try:
            return SubWallet.objects.select_related("wallet", "wallet__user", "department").get(pk=pk)
        except SubWallet.DoesNotExist:
            return None


class SubWalletTransactionRepository:
    """Repository for SubWalletTransaction operations."""

    @staticmethod
    def get_by_sub_wallet(sub_wallet: SubWallet) -> QuerySet[SubWalletTransaction]:
        """Get all transactions for a sub-wallet."""
        return SubWalletTransaction.objects.filter(sub_wallet=sub_wallet).select_related(
            "sub_wallet", "sub_wallet__department"
        )

