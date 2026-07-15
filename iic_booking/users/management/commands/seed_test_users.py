"""
Create/update one test user per UserType for QA.

Usage:
  python manage.py seed_test_users
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from iic_booking.users.models import User
from iic_booking.users.models.department import Department, DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    Wallet,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)
from iic_booking.users.repositories.wallet_repository import (
    get_internal_departments_with_equipment,
)
from iic_booking.users.test_accounts import (
    TEST_USER_PASSWORD,
    test_email_redirect,
    test_user_email_for_type,
)


SEED_WALLET_INR = Decimal("50000.00")


class Command(BaseCommand):
    help = "Idempotently seed is_test_account users for every user type."

    def handle(self, *args, **options):
        seed_depts = list(get_internal_departments_with_equipment())
        # Fallback for assigning a User.department when seed_depts is empty
        internal_dept = (
            seed_depts[0]
            if seed_depts
            else Department.objects.filter(department_type=DepartmentType.INTERNAL)
            .order_by("name")
            .first()
        )
        external_dept = (
            Department.objects.filter(department_type=DepartmentType.EXTERNAL)
            .order_by("name")
            .first()
        )

        created, updated = [], []
        by_type: dict[str, User] = {}
        subwallets_topped = 0

        with transaction.atomic():
            for code, label in UserType.get_choices():
                email = test_user_email_for_type(code)
                name = f"Test {label}"
                defaults = {
                    "name": name,
                    "user_type": code,
                    "is_test_account": True,
                    "email_verified": True,
                    "admin_approved": True,
                    "supervisor_approved": True,
                    "is_active": True,
                    "force_inactive": False,
                    "access_on_hold": False,
                }
                # Staff panel types
                if code in UserType.get_admin_panel_codes():
                    defaults["is_staff"] = code == UserType.ADMIN

                # Attach a department when available
                if code in {
                    UserType.STUDENT,
                    UserType.INDIVIDUAL_STUDENT,
                    UserType.FACULTY,
                    UserType.MANAGER,
                    UserType.OPERATOR,
                    UserType.ADMIN,
                    UserType.FINANCE,
                    UserType.STARTUP_INCUBATED_IITR,
                }:
                    if internal_dept:
                        defaults["department"] = internal_dept
                else:
                    if external_dept:
                        defaults["department"] = external_dept

                user = User.objects.filter(email__iexact=email).first()
                if user:
                    for k, v in defaults.items():
                        setattr(user, k, v)
                    user.set_password(TEST_USER_PASSWORD)
                    # Avoid full_clean wallet constraints surprises on partial profiles
                    user.save()
                    updated.append(email)
                else:
                    user = User(
                        email=email,
                        **{k: v for k, v in defaults.items() if k != "department"},
                    )
                    if "department" in defaults:
                        user.department = defaults["department"]
                    user.set_password(TEST_USER_PASSWORD)
                    user.save()
                    created.append(email)

                by_type[code] = user

            # Ensure wallets + seed balance for wallet-owning types across
            # all internal departments that have equipment (plus General).
            for code, _label in UserType.get_choices():
                user = by_type[code]
                if not user.can_have_wallet():
                    continue
                wallet, _ = Wallet.objects.get_or_create(user=user)
                if not seed_depts:
                    continue
                for dept in seed_depts:
                    sw, _ = SubWallet.objects.get_or_create(
                        wallet=wallet,
                        department=dept,
                        defaults={"balance": Decimal("0.00")},
                    )
                    if Decimal(str(sw.balance or 0)) < SEED_WALLET_INR:
                        need = SEED_WALLET_INR - Decimal(str(sw.balance or 0))
                        sw.credit(
                            need,
                            description="Seed credit for test account",
                            related_user=user,
                        )
                        subwallets_topped += 1

            # Link student → faculty wallet
            student = by_type.get(UserType.STUDENT)
            faculty = by_type.get(UserType.FACULTY)
            if student and faculty:
                faculty_wallet, _ = Wallet.objects.get_or_create(user=faculty)
                join, created_join = WalletJoinRequest.objects.get_or_create(
                    student=student,
                    faculty=faculty,
                    defaults={
                        "wallet": faculty_wallet,
                        "status": WalletJoinRequestStatus.APPROVED,
                        "message": "Seeded test join",
                        "responded_at": timezone.now(),
                    },
                )
                if not created_join:
                    join.wallet = faculty_wallet
                    join.status = WalletJoinRequestStatus.APPROVED
                    if not join.responded_at:
                        join.responded_at = timezone.now()
                    join.save(
                        update_fields=["wallet", "status", "responded_at", "updated_at"]
                    )

        self.stdout.write(self.style.SUCCESS("Test users ready."))
        self.stdout.write(f"  Created: {len(created)}")
        self.stdout.write(f"  Updated: {len(updated)}")
        self.stdout.write(f"  Seed departments: {len(seed_depts)}")
        self.stdout.write(f"  Sub-wallets topped up: {subwallets_topped}")
        self.stdout.write(f"  Shared password: {TEST_USER_PASSWORD}")
        self.stdout.write(f"  Email redirect target: {test_email_redirect()}")
        self.stdout.write("")
        self.stdout.write("Accounts:")
        for code, label in UserType.get_choices():
            self.stdout.write(f"  - {label}: {test_user_email_for_type(code)}")
        if not seed_depts:
            self.stdout.write(
                self.style.WARNING(
                    "No equipment-linked internal departments found — wallets were not credited."
                )
            )
