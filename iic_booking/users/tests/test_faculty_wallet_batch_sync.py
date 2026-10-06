"""Batch faculty wallet sync (daily 02:30 IST run and sync_faculty_legacy_wallets command)."""

from datetime import datetime, timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from iic_booking.users.legacy_ledger import faculty_wallet_batch_sync as batch
from iic_booking.users.legacy_ledger.faculty_login_wallet_sync import sync_faculty_wallet_from_legacy
from iic_booking.users.legacy_ledger.faculty_wallet_sync_deadline import faculty_wallet_sync_deadline_status
from iic_booking.users.legacy_ledger.fake_reader import FakeOldMySQLReader
from iic_booking.users.legacy_ledger.opening_balance import IIC_DEPARTMENT_NAME
from iic_booking.users.models import Department, DepartmentType, SubWallet, SubWalletTransaction, UserType
from iic_booking.users.models.portal_migration import LegacyWalletAccountMapping, PortalMigrationState
from iic_booking.users.tasks import faculty_wallet_daily_sync
from iic_booking.users.tests.factories import UserFactory

IST = ZoneInfo("Asia/Kolkata")


class _Reader(FakeOldMySQLReader):
    def connect(self):
        return self

    def close(self):
        return None


def _txn(tid, legacy_uid, amount, kind, balance):
    return {
        "id": tid,
        "user_id": legacy_uid,
        "amount": str(amount),
        "balance": str(balance),
        "transaction_type": kind,
        "create_date": datetime(2026, 9, 1, 10, tid % 60, tzinfo=IST),
        "description": "Wallet credit",
    }


@pytest.mark.django_db
class TestFacultyWalletBatchSync(TestCase):
    def setUp(self):
        cache.delete(batch.LOCK_KEY)
        self._open_window()
        Department.objects.create(name=IIC_DEPARTMENT_NAME, department_type=DepartmentType.INTERNAL)
        self.a = UserFactory(email="batch.a@iitr.ac.in", name="Prof Alpha", user_type=UserType.FACULTY, emp_id="770061")
        self.b = UserFactory(email="batch.b@iitr.ac.in", name="Prof Beta", user_type=UserType.FACULTY, emp_id="770062")
        self.users = [
            {"id": 51, "emp_id": "770061", "name": "Old Alpha", "email": "old.a@x"},
            {"id": 52, "emp_id": "770062", "name": "Old Beta", "email": "old.b@x"},
        ]
        self.txns = [_txn(1, 51, "1000.00", 1, "1000.00"), _txn(2, 52, "500.00", 1, "500.00")]
        self.balances = {51: "1000.00", 52: "500.00"}

    def _open_window(self):
        PortalMigrationState.objects.update_or_create(
            singleton_key="default", defaults={"faculty_wallet_sync_cutoff": timezone.now() + timedelta(days=5)}
        )

    def _reader(self):
        return _Reader(
            users=self.users,
            wallets={uid: {"id": uid + 100, "user_id": uid, "balance": Decimal(b)} for uid, b in self.balances.items()},
            transactions=list(self.txns),
        )

    def _run(self, apply=True, **kw):
        return batch.run_faculty_wallet_batch_sync(apply=apply, reader=self._reader(), **kw)

    def _sub(self, user):
        return SubWallet.objects.get(wallet__user=user, department__name=IIC_DEPARTMENT_NAME)

    def _txn_count(self):
        return SubWalletTransaction.objects.count()

    def test_window_closed_does_nothing(self):
        PortalMigrationState.objects.filter(singleton_key="default").update(
            faculty_wallet_sync_cutoff=timezone.now() - timedelta(minutes=1)
        )
        summary = self._run()
        self.assertEqual(summary["status"], "window_closed")
        self.assertEqual(summary["checked"], 0)
        self.assertEqual(self._txn_count(), 0)

    def test_dry_run_writes_nothing_then_apply_is_idempotent(self):
        dry = self._run(apply=False)
        self.assertEqual(dry["credits"], {"count": 2, "total": "1500.00"})
        self.assertEqual(self._txn_count(), 0)
        self.assertFalse(LegacyWalletAccountMapping.objects.exists())

        applied = self._run()
        self.assertEqual(applied["credits"], {"count": 2, "total": "1500.00"})
        self.assertEqual(self._sub(self.a).balance, Decimal("1000.00"))
        count = self._txn_count()

        again = self._run()
        self.assertEqual(again["in_sync"], 2)
        self.assertEqual(again["credits"]["count"] + again["debits"]["count"], 0)
        self.assertEqual(self._txn_count(), count)

    def test_shares_markers_with_login_sync(self):
        self._run()
        self.balances[51] = "800.00"
        self.txns.append(_txn(3, 51, "200.00", 2, "800.00"))
        login = sync_faculty_wallet_from_legacy(self.a, reader=self._reader())
        self.assertEqual(login["reconcile"]["delta"], "-200.00")
        summary = self._run()
        self.assertEqual(summary["in_sync"], 2)
        self.assertEqual(self._sub(self.a).balance, Decimal("800.00"))

    def test_deduction_below_zero_is_blocked_and_reported(self):
        self._run()
        SubWallet.objects.filter(pk=self._sub(self.a).pk).update(balance=Decimal("100.00"))
        self.balances[51] = "500.00"
        self.balances[52] = "450.00"
        self.txns += [_txn(3, 51, "500.00", 2, "500.00"), _txn(4, 52, "50.00", 2, "450.00")]
        count = self._txn_count()

        dry = self._run(apply=False)
        self.assertEqual(dry["blocked_below_zero"], [{"user_id": self.a.pk, "delta": "-500.00", "iic_balance": "100.00"}])

        summary = self._run()
        self.assertEqual(summary["blocked_below_zero"], [{"user_id": self.a.pk, "delta": "-500.00", "iic_balance": "100.00"}])
        self.assertEqual(summary["debits"], {"count": 1, "total": "50.00"})
        self.assertEqual(self._sub(self.a).balance, Decimal("100.00"))
        self.assertEqual(self._sub(self.b).balance, Decimal("450.00"))
        self.assertEqual(self._txn_count(), count + 1)

    def test_one_user_failing_does_not_stop_the_run(self):
        real = batch.sync_faculty_wallet_from_legacy

        def flaky(user, **kw):
            if user.pk == self.a.pk:
                raise RuntimeError("boom")
            return real(user, **kw)

        with mock.patch.object(batch, "sync_faculty_wallet_from_legacy", side_effect=flaky):
            summary = self._run()
        self.assertEqual(summary["failed"], [{"user_id": self.a.pk, "reason": "error"}])
        self.assertEqual(summary["credits"], {"count": 1, "total": "500.00"})
        self.assertEqual(self._sub(self.b).balance, Decimal("500.00"))

    def test_admin_mapped_to_other_legacy_user_is_skipped(self):
        LegacyWalletAccountMapping.objects.create(
            employee_id="X-770061", old_user_id=999, new_user=self.a, migration_batch="admin-map:1"
        )
        summary = self._run()
        self.assertEqual(summary["skipped_admin_mapped_user_ids"], [self.a.pk])
        self.assertFalse(SubWallet.objects.filter(wallet__user=self.a, balance__gt=0).exists())

    def test_daily_task_records_last_run_for_admin_page(self):
        with mock.patch.object(batch, "OldMySQLReader", return_value=self._reader()), mock.patch.object(
            batch, "_mysql_configured", return_value=True
        ):
            result = faculty_wallet_daily_sync()
        self.assertEqual(result["status"], "completed")
        last = faculty_wallet_sync_deadline_status()["last_batch_sync"]
        self.assertEqual(last["trigger"], "daily")
        self.assertEqual(last["credits"], {"count": 2, "total": "1500.00"})
        self.assertNotIn("changes", last)
        self.assertIsNotNone(last["ran_at"])

    def test_command_prints_ids_and_amounts_only(self):
        out = StringIO()
        with mock.patch.object(batch, "OldMySQLReader", return_value=self._reader()), mock.patch.object(
            batch, "_mysql_configured", return_value=True
        ):
            call_command("sync_faculty_legacy_wallets", "--user-ids", str(self.a.pk), stdout=out)
        text = out.getvalue()
        self.assertIn(f"WOULD POST user_id={self.a.pk} credit delta=1000.00", text)
        self.assertNotIn(f"user_id={self.b.pk}", text)
        for private in ("batch.a@", "Prof Alpha", "Old Alpha", "old.a@"):
            self.assertNotIn(private, text)
        self.assertEqual(self._txn_count(), 0)

    def test_command_fails_loudly_when_window_closed(self):
        PortalMigrationState.objects.filter(singleton_key="default").update(
            faculty_wallet_sync_cutoff=timezone.now() - timedelta(minutes=1)
        )
        with self.assertRaises(CommandError):
            call_command("sync_faculty_legacy_wallets", "--apply", stdout=StringIO())
