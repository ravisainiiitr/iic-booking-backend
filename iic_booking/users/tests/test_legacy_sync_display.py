"""Plain-language wallet description for legacy-portal balance sync rows (faculty login sync)."""

from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from django.test import TestCase
from django.utils import timezone

from iic_booking.users.legacy_ledger.faculty_login_wallet_sync import sync_faculty_wallet_from_legacy
from iic_booking.users.legacy_ledger.fake_reader import FakeOldMySQLReader
from iic_booking.users.legacy_ledger.opening_balance import (
    IIC_DEPARTMENT_NAME,
    legacy_sync_display,
    reconcile_legacy_balance_to_subwallet,
)
from iic_booking.users.models import Department, DepartmentType, SubWalletTransaction, UserType
from iic_booking.users.models.portal_migration import PortalMigrationState
from iic_booking.users.serializers.wallet_serializer import SubWalletTransactionSerializer
from iic_booking.users.tests.factories import UserFactory

IST = ZoneInfo("Asia/Kolkata")


def _txn(tid, amount, kind, when, balance):
    return {
        "id": tid,
        "user_id": 41,
        "amount": str(amount),
        "balance": str(balance),
        "transaction_type": kind,
        "create_date": when,
        "description": "Paid for XPS Booking.",
    }


@pytest.mark.django_db
class TestLegacySyncDisplay(TestCase):
    def setUp(self):
        PortalMigrationState.objects.update_or_create(
            singleton_key="default", defaults={"faculty_wallet_sync_cutoff": timezone.now() + timedelta(days=5)}
        )
        self.iic = Department.objects.create(name=IIC_DEPARTMENT_NAME, department_type=DepartmentType.INTERNAL)
        self.user = UserFactory(
            email="fac.display@iitr.ac.in", user_type=UserType.FACULTY, emp_id="770055", admin_approved=True
        )
        self.users = [{"id": 41, "emp_id": "770055", "name": "Old Prof", "email": "old@x"}]
        self.txns = [
            _txn(1, "2000.00", 1, datetime(2026, 9, 1, 10, 0, tzinfo=IST), "2000.00"),
            _txn(2, "100.00", 2, datetime(2026, 9, 2, 10, 0, tzinfo=IST), "1900.00"),
        ]

    def _sync(self, balance):
        reader = FakeOldMySQLReader(
            users=self.users,
            wallets={41: {"id": 9, "user_id": 41, "balance": Decimal(balance)}},
            transactions=list(self.txns),
        )
        result = sync_faculty_wallet_from_legacy(self.user, reader=reader)
        self.assertTrue(result["ok"], result)
        return SubWalletTransaction.objects.filter(sub_wallet__wallet__user=self.user).order_by("-id").first()

    def test_first_sync_reads_as_carried_over_balance(self):
        txn = self._sync("1900.00")
        self.assertEqual(
            legacy_sync_display(txn), "Balance carried over from the old IIC portal: ₹1,900.00"
        )

    def test_later_debit_names_the_old_portal_charges(self):
        self._sync("1900.00")
        self.txns += [
            _txn(3, "100.00", 2, datetime(2026, 9, 22, 19, 44, tzinfo=IST), "1800.00"),
            _txn(4, "400.00", 2, datetime(2026, 9, 28, 10, 13, tzinfo=IST), "1400.00"),
        ]
        txn = self._sync("1400.00")
        self.assertEqual(txn.transaction_type, "debit")
        self.assertEqual(txn.amount, Decimal("500.00"))
        self.assertEqual(
            legacy_sync_display(txn),
            "Old IIC portal balance update: ₹500.00 deducted for 2 charges made on the old portal between "
            "22 Sep 2026 and 28 Sep 2026, after its balance was last copied here. "
            "Old portal balance now ₹1,400.00.",
        )
        data = SubWalletTransactionSerializer(txn).data
        self.assertEqual(data["description_display"], legacy_sync_display(txn))
        self.assertIn("delta=-500.00", data["description"])

    def test_mixed_charges_and_credits(self):
        self._sync("1900.00")
        self.txns += [
            _txn(3, "300.00", 2, datetime(2026, 9, 25, 9, 0, tzinfo=IST), "1600.00"),
            _txn(4, "100.00", 1, datetime(2026, 9, 25, 15, 0, tzinfo=IST), "1700.00"),
        ]
        txn = self._sync("1700.00")
        self.assertEqual(
            legacy_sync_display(txn),
            "Old IIC portal balance update: ₹200.00 deducted for 1 charge of ₹300.00 and 1 credit of ₹100.00 "
            "made on the old portal on 25 Sep 2026, after its balance was last copied here. "
            "Old portal balance now ₹1,700.00.",
        )

    def test_without_matching_ledger_rows_falls_back_to_generic_text(self):
        reconcile_legacy_balance_to_subwallet(
            user=self.user, target_balance=Decimal("1000.00"), migration_id="faculty-wallet:770055"
        )
        txn = reconcile_legacy_balance_to_subwallet(
            user=self.user, target_balance=Decimal("750.00"), migration_id="faculty-wallet:770055"
        ).transaction
        self.assertEqual(
            legacy_sync_display(txn),
            "Old IIC portal balance update: the old portal balance fell by ₹250.00 since it was last copied here. "
            "Old portal balance now ₹750.00.",
        )

    def test_other_transactions_are_untouched(self):
        txn = self._sync("1900.00")
        txn.description = "Booking #XPS - X-ray Photoelectron Spectroscopy (XPS) (180 minutes)"
        self.assertIsNone(legacy_sync_display(txn))
