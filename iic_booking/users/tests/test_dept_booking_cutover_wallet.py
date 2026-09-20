"""Department booking gate, cutover lock, and faculty login wallet sync."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone

from iic_booking.users.legacy_ledger.booking_lock import (
    FACULTY_WALLET_SYNC_CUTOFF,
    booking_is_locked,
    department_equipment_booking_blocked,
    faculty_wallet_sync_window_open,
    portal_hard_freeze_active,
)
from iic_booking.users.legacy_ledger.fake_reader import FakeOldMySQLReader
from iic_booking.users.legacy_ledger.faculty_login_wallet_sync import (
    faculty_login_migration_id,
    sync_faculty_wallet_from_legacy,
)
from iic_booking.users.legacy_ledger.opening_balance import (
    IIC_DEPARTMENT_NAME,
    OpeningBalanceError,
    create_migration_opening_balance,
    get_iic_department,
    reconcile_legacy_balance_to_subwallet,
)
from iic_booking.users.models import Department, DepartmentType, SubWallet, UserType
from iic_booking.users.models.portal_migration import (
    LegacyWalletLedgerEntry,
    PortalMigrationState,
)
from iic_booking.users.tests.factories import UserFactory


def _opens_at(days_offset: int = 0):
    return timezone.now() + timedelta(days=days_offset)


@pytest.mark.django_db
class TestDepartmentEquipmentBookingGate(TestCase):
    def test_missing_department_blocks(self):
        class Eq:
            internal_department = None

        blocked, msg = department_equipment_booking_blocked(Eq())
        self.assertTrue(blocked)
        self.assertIn("not linked", msg.lower())

    def test_disabled_department_blocks(self):
        dept = Department.objects.create(
            name="Chem",
            department_type=DepartmentType.INTERNAL,
            equipment_booking_enabled=False,
        )

        class Eq:
            internal_department = dept

        blocked, msg = department_equipment_booking_blocked(Eq())
        self.assertTrue(blocked)
        self.assertIn("Chem", msg)

    def test_enabled_department_allows(self):
        dept = Department.objects.create(
            name="Phys",
            department_type=DepartmentType.INTERNAL,
            equipment_booking_enabled=True,
        )

        class Eq:
            internal_department = dept

        blocked, msg = department_equipment_booking_blocked(Eq())
        self.assertFalse(blocked)
        self.assertEqual(msg, "")

    def test_new_department_defaults_booking_off(self):
        dept = Department.objects.create(
            name="NewDept",
            department_type=DepartmentType.INTERNAL,
        )
        self.assertFalse(dept.equipment_booking_enabled)


@pytest.mark.django_db
class TestGlobalBookingFreeze(TestCase):
    def setUp(self):
        self.state = PortalMigrationState.get_solo()
        self.state.end_user_booking_enabled = False
        self.state.booking_opens_at = _opens_at(30)
        self.state.save()

    def test_hard_freeze_locks_faculty_and_admin(self):
        self.assertTrue(portal_hard_freeze_active())
        faculty = UserFactory(email="f@iitr.ac.in", user_type=UserType.FACULTY)
        admin = UserFactory(email="a@iitr.ac.in", user_type=UserType.ADMIN)
        self.assertTrue(booking_is_locked(faculty)[0])
        self.assertTrue(booking_is_locked(admin)[0])

    def test_after_opens_staff_unlocked_end_user_still_locked(self):
        self.state.booking_opens_at = _opens_at(-1)
        self.state.end_user_booking_enabled = False
        self.state.save()
        self.assertFalse(portal_hard_freeze_active())
        faculty = UserFactory(email="f2@iitr.ac.in", user_type=UserType.FACULTY)
        admin = UserFactory(email="a2@iitr.ac.in", user_type=UserType.ADMIN)
        self.assertTrue(booking_is_locked(faculty)[0])
        self.assertFalse(booking_is_locked(admin)[0])

    def test_after_opens_and_enabled_faculty_unlocked(self):
        self.state.booking_opens_at = _opens_at(-1)
        self.state.end_user_booking_enabled = True
        self.state.save()
        faculty = UserFactory(email="f3@iitr.ac.in", user_type=UserType.FACULTY)
        self.assertFalse(booking_is_locked(faculty)[0])


@pytest.mark.django_db
class TestOpeningBalanceIicTarget(TestCase):
    def setUp(self):
        self.iic = Department.objects.create(
            name=IIC_DEPARTMENT_NAME,
            department_type=DepartmentType.INTERNAL,
        )
        self.user = UserFactory(
            email="fac.ob@iitr.ac.in",
            user_type=UserType.FACULTY,
            emp_id="EMP100",
            admin_approved=True,
        )

    def test_get_iic_department(self):
        self.assertEqual(get_iic_department().pk, self.iic.pk)

    def test_opening_balance_to_iic_department(self):
        txn = create_migration_opening_balance(
            user=self.user,
            amount=Decimal("25.00"),
            migration_id="iic-ob-1",
            department=self.iic,
        )
        self.assertEqual(txn.sub_wallet.department_id, self.iic.pk)
        with self.assertRaises(OpeningBalanceError):
            create_migration_opening_balance(
                user=self.user,
                amount=Decimal("25.00"),
                migration_id="iic-ob-1",
                department=self.iic,
            )

    def test_reconcile_idempotent_then_delta(self):
        mid = faculty_login_migration_id(user_id=self.user.pk, employee_id="EMP100")
        r1 = reconcile_legacy_balance_to_subwallet(
            user=self.user,
            target_balance=Decimal("40.00"),
            migration_id=mid,
            department=self.iic,
        )
        self.assertTrue(r1.created)
        self.assertEqual(r1.delta, Decimal("40.00"))
        r2 = reconcile_legacy_balance_to_subwallet(
            user=self.user,
            target_balance=Decimal("40.00"),
            migration_id=mid,
            department=self.iic,
        )
        self.assertFalse(r2.created)
        self.assertEqual(r2.delta, Decimal("0.00"))
        r3 = reconcile_legacy_balance_to_subwallet(
            user=self.user,
            target_balance=Decimal("55.00"),
            migration_id=mid,
            department=self.iic,
        )
        self.assertTrue(r3.created)
        self.assertEqual(r3.delta, Decimal("15.00"))
        sub = SubWallet.objects.get(wallet__user=self.user, department=self.iic)
        self.assertEqual(sub.balance, Decimal("55.00"))


@pytest.mark.django_db
class TestFacultyLoginWalletSync(TestCase):
    def setUp(self):
        self.iic = Department.objects.create(
            name=IIC_DEPARTMENT_NAME,
            department_type=DepartmentType.INTERNAL,
        )
        self.user = UserFactory(
            email="fac.sync@iitr.ac.in",
            user_type=UserType.FACULTY,
            emp_id="654321",
            admin_approved=True,
        )
        self.reader = FakeOldMySQLReader(
            users=[{"id": 10, "emp_id": "654321", "name": "Old", "email": "old@x"}],
            wallets={10: {"id": 3, "user_id": 10, "balance": Decimal("40.00")}},
            transactions=[
                {
                    "id": 100,
                    "user_id": 10,
                    "amount": "50.00",
                    "balance": "50.00",
                    "transaction_type": 1,
                    "create_date": timezone.now(),
                    "description": "Recharge UTR: AAA",
                },
                {
                    "id": 101,
                    "user_id": 10,
                    "amount": "10.00",
                    "balance": "40.00",
                    "transaction_type": 2,
                    "create_date": timezone.now(),
                    "description": "Booking #99",
                },
            ],
        )

    def test_sync_imports_and_credits_iic(self):
        result = sync_faculty_wallet_from_legacy(self.user, reader=self.reader)
        self.assertTrue(result["ok"])
        self.assertEqual(LegacyWalletLedgerEntry.objects.filter(employee_id="654321").count(), 2)
        sub = SubWallet.objects.get(wallet__user=self.user, department=self.iic)
        self.assertEqual(sub.balance, Decimal("40.00"))
        result2 = sync_faculty_wallet_from_legacy(self.user, reader=self.reader)
        self.assertTrue(result2["ok"])
        self.assertEqual(result2["reconcile"]["delta"], "0.00")
        self.assertEqual(SubWallet.objects.get(pk=sub.pk).balance, Decimal("40.00"))

    def test_sync_skipped_after_cutover(self):
        with patch(
            "iic_booking.users.legacy_ledger.faculty_login_wallet_sync.faculty_wallet_sync_window_open",
            return_value=False,
        ):
            result = sync_faculty_wallet_from_legacy(self.user, reader=self.reader)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "after_cutover")
        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 0)

    def test_non_faculty_skipped(self):
        student = UserFactory(email="s@iitr.ac.in", user_type=UserType.STUDENT, emp_id="STU654321")
        result = sync_faculty_wallet_from_legacy(student, reader=self.reader)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "not_faculty")

    def test_cutoff_constant(self):
        self.assertEqual(FACULTY_WALLET_SYNC_CUTOFF.year, 2026)
        self.assertEqual(FACULTY_WALLET_SYNC_CUTOFF.month, 10)
        self.assertEqual(FACULTY_WALLET_SYNC_CUTOFF.day, 4)
        self.assertIsInstance(faculty_wallet_sync_window_open(), bool)
