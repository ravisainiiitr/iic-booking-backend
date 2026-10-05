"""Main Administrator faculty login wallet sync deadline: window logic, audited service, API, login gate."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from django.db import connection
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.booking_lock import (
    FACULTY_WALLET_SYNC_CUTOFF,
    faculty_wallet_sync_cutoff,
    faculty_wallet_sync_window_open,
)
from iic_booking.users.legacy_ledger.fake_reader import FakeOldMySQLReader
from iic_booking.users.legacy_ledger.faculty_login_wallet_sync import (
    maybe_sync_faculty_wallet_on_login,
    sync_faculty_wallet_from_legacy,
)
from iic_booking.users.legacy_ledger.faculty_wallet_sync_deadline import (
    FacultyWalletSyncDeadlineError,
    faculty_wallet_sync_deadline_status,
    set_faculty_wallet_sync_cutoff,
)
from iic_booking.users.legacy_ledger.opening_balance import IIC_DEPARTMENT_NAME
from iic_booking.users.models import Department, DepartmentType, SubWallet, UserType
from iic_booking.users.models.portal_migration import (
    FacultyWalletSyncCutoffChange,
    LegacyWalletLedgerEntry,
    PortalMigrationState,
)
from iic_booking.users.tests.factories import UserFactory

IST = ZoneInfo("Asia/Kolkata")
URL = "/api/portal-migration/admin/faculty-wallet-sync/"
NOW_PATH = "iic_booking.users.legacy_ledger.booking_lock.timezone.now"


def _store(value):
    PortalMigrationState.objects.update_or_create(
        singleton_key="default", defaults={"faculty_wallet_sync_cutoff": value}
    )


@pytest.mark.django_db
class TestWindowLogic(TestCase):
    def test_no_state_row_falls_back_to_constant(self):
        PortalMigrationState.objects.all().delete()
        self.assertEqual(faculty_wallet_sync_cutoff(), FACULTY_WALLET_SYNC_CUTOFF)

    def test_null_setting_uses_constant(self):
        _store(None)
        with patch(NOW_PATH, return_value=FACULTY_WALLET_SYNC_CUTOFF - timedelta(seconds=1)):
            self.assertTrue(faculty_wallet_sync_window_open())
        with patch(NOW_PATH, return_value=FACULTY_WALLET_SYNC_CUTOFF + timedelta(seconds=1)):
            self.assertFalse(faculty_wallet_sync_window_open())

    def test_stored_value_overrides_constant(self):
        cutoff = datetime(2026, 10, 31, 23, 59, 59, tzinfo=IST)
        _store(cutoff)
        self.assertEqual(faculty_wallet_sync_cutoff(), cutoff)
        with patch(NOW_PATH, return_value=cutoff - timedelta(seconds=1)):
            self.assertTrue(faculty_wallet_sync_window_open())
        with patch(NOW_PATH, return_value=cutoff):
            self.assertFalse(faculty_wallet_sync_window_open())
        with patch(NOW_PATH, return_value=cutoff + timedelta(days=1)):
            self.assertFalse(faculty_wallet_sync_window_open())

    def test_earlier_stored_value_closes_before_constant(self):
        cutoff = FACULTY_WALLET_SYNC_CUTOFF - timedelta(days=2)
        _store(cutoff)
        with patch(NOW_PATH, return_value=cutoff + timedelta(hours=1)):
            self.assertFalse(faculty_wallet_sync_window_open())

    def test_database_error_falls_back_and_keeps_transaction_usable(self):
        _store(timezone.now() + timedelta(days=10))

        def missing_column(*args, **kwargs):
            with connection.cursor() as cur:
                cur.execute("SELECT faculty_wallet_sync_cutoff_missing FROM users_portalmigrationstate")

        with patch.object(PortalMigrationState.objects, "filter", side_effect=missing_column):
            self.assertEqual(faculty_wallet_sync_cutoff(), FACULTY_WALLET_SYNC_CUTOFF)
        # TestCase runs inside a transaction: a poisoned transaction would fail this query.
        self.assertEqual(PortalMigrationState.objects.count(), 1)


@pytest.mark.django_db
class TestDeadlineService(TestCase):
    def setUp(self):
        self.admin = UserFactory(email="main.admin.fws@iitr.ac.in", user_type=UserType.ADMIN, name="Main Admin")
        self.now = datetime(2026, 10, 5, 10, 0, tzinfo=IST)

    def test_set_writes_value_and_audit(self):
        change = set_faculty_wallet_sync_cutoff(
            actor=self.admin, raw_cutoff="2026-10-31T23:59:59+05:30", reason="Extend to month end", now=self.now
        )
        state = PortalMigrationState.objects.get(singleton_key="default")
        expected = datetime(2026, 10, 31, 23, 59, 59, tzinfo=IST)
        self.assertEqual(state.faculty_wallet_sync_cutoff, expected)
        self.assertIsNone(change.old_cutoff)
        self.assertEqual(change.new_cutoff, expected)
        self.assertEqual(change.actor_id, self.admin.pk)
        self.assertEqual(change.actor_email, self.admin.email)
        self.assertEqual(change.reason, "Extend to month end")

        second = set_faculty_wallet_sync_cutoff(
            actor=self.admin, raw_cutoff="2026-10-20T18:00:00+05:30", reason="Shorten", now=self.now
        )
        self.assertEqual(second.old_cutoff, expected)
        self.assertEqual(FacultyWalletSyncCutoffChange.objects.count(), 2)

    def test_reason_required(self):
        for reason in (None, "", "   "):
            with self.assertRaises(FacultyWalletSyncDeadlineError):
                set_faculty_wallet_sync_cutoff(
                    actor=self.admin, raw_cutoff="2026-10-31T23:59:59+05:30", reason=reason, now=self.now
                )
        self.assertFalse(FacultyWalletSyncCutoffChange.objects.exists())

    def test_naive_and_malformed_values_rejected(self):
        for raw in ("2026-10-31T23:59:59", "31/10/2026", "not a date", "2026-13-40T00:00:00+05:30"):
            with self.assertRaises(FacultyWalletSyncDeadlineError):
                set_faculty_wallet_sync_cutoff(actor=self.admin, raw_cutoff=raw, reason="r", now=self.now)
        self.assertFalse(FacultyWalletSyncCutoffChange.objects.exists())

    def test_more_than_one_year_ahead_rejected(self):
        too_far = (self.now + timedelta(days=366)).isoformat()
        with self.assertRaises(FacultyWalletSyncDeadlineError):
            set_faculty_wallet_sync_cutoff(actor=self.admin, raw_cutoff=too_far, reason="r", now=self.now)
        ok = (self.now + timedelta(days=364)).isoformat()
        set_faculty_wallet_sync_cutoff(actor=self.admin, raw_cutoff=ok, reason="r", now=self.now)

    def test_empty_value_closes_now(self):
        _store(timezone.now() + timedelta(days=10))
        self.assertTrue(faculty_wallet_sync_window_open())
        change = set_faculty_wallet_sync_cutoff(actor=self.admin, raw_cutoff="", reason="Close the sync")
        self.assertLessEqual(change.new_cutoff, timezone.now())
        self.assertFalse(faculty_wallet_sync_window_open())

    def test_status_reports_source_and_last_change(self):
        status = faculty_wallet_sync_deadline_status(now=self.now)
        self.assertEqual(status["source"], "default")
        self.assertIsNone(status["stored_cutoff"])
        self.assertFalse(status["window_open"])
        self.assertIsNone(status["last_change"])

        set_faculty_wallet_sync_cutoff(
            actor=self.admin, raw_cutoff="2026-10-31T23:59:59+05:30", reason="Extend", now=self.now
        )
        status = faculty_wallet_sync_deadline_status(now=self.now)
        self.assertEqual(status["source"], "setting")
        self.assertEqual(status["cutoff"], "2026-10-31T23:59:59+05:30")
        self.assertEqual(status["cutoff_ist"], "31 Oct 2026, 23:59:59 IST")
        self.assertTrue(status["window_open"])
        self.assertEqual(status["last_change"]["changed_by_email"], self.admin.email)
        self.assertEqual(status["last_change"]["changed_by_name"], "Main Admin")
        self.assertEqual(status["last_change"]["reason"], "Extend")
        self.assertIsNone(status["last_change"]["old_cutoff"])


@pytest.mark.django_db
class TestDeadlineApi(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = UserFactory(email="main.admin.api@iitr.ac.in", user_type=UserType.ADMIN)

    def _future(self, days=10):
        return (timezone.now() + timedelta(days=days)).astimezone(IST).replace(microsecond=0).isoformat()

    def test_non_admin_forbidden(self):
        dept = Department.objects.create(name="Chem FWS", department_type=DepartmentType.INTERNAL)
        others = [
            UserFactory(email="fac.api@iitr.ac.in", user_type=UserType.FACULTY),
            UserFactory(email="da.api@iitr.ac.in", user_type=UserType.DEPT_ADMIN, department=dept),
            UserFactory(email="oic.api@iitr.ac.in", user_type=UserType.MANAGER),
            UserFactory(email="su.api@iitr.ac.in", user_type=UserType.STUDENT, is_superuser=True),
        ]
        for user in others:
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(URL).status_code, 403)
            res = self.client.put(URL, {"cutoff": self._future(), "reason": "x"}, format="json")
            self.assertEqual(res.status_code, 403)
        self.client.force_authenticate(None)
        self.assertIn(self.client.get(URL).status_code, (401, 403))
        self.assertFalse(FacultyWalletSyncCutoffChange.objects.exists())

    def test_admin_get(self):
        self.client.force_authenticate(self.admin)
        res = self.client.get(URL)
        self.assertEqual(res.status_code, 200)
        for key in ("cutoff", "cutoff_ist", "source", "window_open", "last_change", "default_cutoff", "max_cutoff"):
            self.assertIn(key, res.data)

    def test_put_requires_reason(self):
        self.client.force_authenticate(self.admin)
        res = self.client.put(URL, {"cutoff": self._future()}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("reason", res.data["error"].lower())
        self.assertFalse(FacultyWalletSyncCutoffChange.objects.exists())

    def test_post_requires_cutoff_key(self):
        self.client.force_authenticate(self.admin)
        res = self.client.post(URL, {"reason": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(FacultyWalletSyncCutoffChange.objects.exists())

    def test_put_sets_deadline_and_audits(self):
        self.client.force_authenticate(self.admin)
        cutoff = self._future(12)
        res = self.client.put(URL, {"cutoff": cutoff, "reason": "Extend per Main Admin"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["cutoff"], cutoff)
        self.assertTrue(res.data["window_open"])
        self.assertEqual(res.data["last_change"]["changed_by_email"], self.admin.email)
        change = FacultyWalletSyncCutoffChange.objects.get()
        self.assertEqual(change.actor_id, self.admin.pk)
        self.assertEqual(change.reason, "Extend per Main Admin")
        self.assertTrue(faculty_wallet_sync_window_open())

    def test_post_empty_cutoff_closes_window(self):
        _store(timezone.now() + timedelta(days=5))
        self.client.force_authenticate(self.admin)
        res = self.client.post(URL, {"cutoff": "", "reason": "Close now"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertFalse(res.data["window_open"])
        self.assertFalse(faculty_wallet_sync_window_open())

    def test_put_rejects_naive_and_far_future(self):
        self.client.force_authenticate(self.admin)
        res = self.client.put(URL, {"cutoff": "2026-10-31T23:59:59", "reason": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.put(URL, {"cutoff": self._future(400), "reason": "x"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(FacultyWalletSyncCutoffChange.objects.exists())


class _ConnectingFakeReader(FakeOldMySQLReader):
    def connect(self):
        return self

    def close(self):
        return None


@pytest.mark.django_db
class TestLoginSyncGate(TestCase):
    def setUp(self):
        self.iic = Department.objects.create(name=IIC_DEPARTMENT_NAME, department_type=DepartmentType.INTERNAL)
        self.user = UserFactory(
            email="fac.gate@iitr.ac.in", user_type=UserType.FACULTY, emp_id="880011", admin_approved=True
        )
        self.reader = _ConnectingFakeReader(
            users=[{"id": 31, "emp_id": "880011", "name": "Old Prof", "email": "old.prof@x"}],
            wallets={31: {"id": 7, "user_id": 31, "balance": Decimal("75.00")}},
            transactions=[
                {
                    "id": 3100,
                    "user_id": 31,
                    "amount": "75.00",
                    "balance": "75.00",
                    "transaction_type": 1,
                    "create_date": timezone.now(),
                    "description": "Recharge UTR: GATE",
                },
            ],
        )

    def test_runs_before_stored_deadline(self):
        _store(timezone.now() + timedelta(days=3))
        result = sync_faculty_wallet_from_legacy(self.user, reader=self.reader)
        self.assertTrue(result["ok"], result)
        self.assertEqual(LegacyWalletLedgerEntry.objects.filter(employee_id="880011").count(), 1)
        self.assertEqual(SubWallet.objects.get(wallet__user=self.user, department=self.iic).balance, Decimal("75.00"))

    def test_skipped_after_stored_deadline(self):
        _store(timezone.now() - timedelta(minutes=1))
        result = sync_faculty_wallet_from_legacy(self.user, reader=self.reader)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "after_cutover")
        self.assertFalse(LegacyWalletLedgerEntry.objects.exists())
        self.assertFalse(SubWallet.objects.filter(wallet__user=self.user).exists())

    def test_login_hook_uses_deadline(self):
        target = "iic_booking.users.legacy_ledger.faculty_login_wallet_sync"
        with patch(f"{target}.OldMySQLReader", return_value=self.reader), patch(
            f"{target}._mysql_configured", return_value=True
        ):
            _store(timezone.now() - timedelta(minutes=1))
            maybe_sync_faculty_wallet_on_login(self.user)
            self.assertFalse(LegacyWalletLedgerEntry.objects.exists())

            _store(timezone.now() + timedelta(days=1))
            maybe_sync_faculty_wallet_on_login(self.user)
            self.assertEqual(LegacyWalletLedgerEntry.objects.filter(employee_id="880011").count(), 1)
