"""Main Administrator legacy user mapping: test sync (read-only) and confirmed wallet + booking sync."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentStatus
from iic_booking.users.legacy_ledger.admin_user_sync import (
    AdminSyncError,
    TARGET_LINKED_FACULTY,
    apply_sync,
    build_sync_preview,
)
from iic_booking.users.legacy_ledger.faculty_login_wallet_sync import sync_faculty_wallet_from_legacy
from iic_booking.users.legacy_ledger.opening_balance import IIC_DEPARTMENT_NAME
from iic_booking.users.models import Department, DepartmentType, SubWallet, SubWalletTransaction, UserType, Wallet
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingHistoryRecord,
    LegacyWalletAccountMapping,
    LegacyWalletLedgerEntry,
    PortalMigrationState,
)
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.factories import UserFactory


class SqlFakeReader:
    """Answers the read-only SQL used by admin_user_sync from in-memory rows."""

    def __init__(self, users, wallets=None, transactions=None, bookings=None):
        self.users = {int(u["id"]): u for u in users}
        self.wallets = wallets or {}
        self.transactions = transactions or []
        self.bookings = bookings or []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def _txns(self, uid):
        return [t for t in self.transactions if int(t["user_id"]) == int(uid)]

    def fetchone(self, sql, params=()):
        rows = self.fetchall(sql, params)
        return rows[0] if rows else None

    def fetchall(self, sql, params=()):
        s = " ".join(sql.split())
        if s == "SHOW TABLES":
            return [{"t": "users"}, {"t": "user_wallet"}, {"t": "wallet_transactions"}, {"t": "booking"}]
        if s.startswith("SHOW COLUMNS FROM `booking`"):
            keys = set().union(*[b.keys() for b in self.bookings]) if self.bookings else {"id", "user_id"}
            return [{"Field": k} for k in keys]
        if s.startswith("SELECT * FROM users WHERE id = %s"):
            u = self.users.get(int(params[0]))
            return [u] if u else []
        if s.startswith("SELECT COUNT(*) AS n FROM users WHERE TRIM(emp_id)"):
            return [{"n": sum(1 for u in self.users.values() if str(u.get("emp_id") or "").strip() == params[0])}]
        if s.startswith("SELECT id, emp_id, name, email FROM users WHERE"):
            return list(self.users.values())
        if s.startswith("SELECT COUNT(*) AS n FROM wallet_transactions"):
            return [{"n": len(self._txns(params[0]))}]
        if "FROM wallet_transactions WHERE user_id = %s ORDER BY id DESC" in s:
            return sorted(self._txns(params[0]), key=lambda t: -int(t["id"]))[:10]
        if s.startswith("SELECT * FROM `booking` WHERE `user_id` = %s"):
            rows = [b for b in self.bookings if int(b["user_id"]) == int(params[0])]
            return sorted(rows, key=lambda b: -int(b["id"]))[: int(params[1])]
        raise AssertionError(f"Unexpected SQL in fake reader: {s}")

    def wallet_for_user(self, uid):
        return self.wallets.get(int(uid))

    def user_ledger_totals(self, uid):
        credits = sum(Decimal(str(t["amount"])) for t in self._txns(uid) if int(t["transaction_type"]) == 1)
        debits = sum(Decimal(str(t["amount"])) for t in self._txns(uid) if int(t["transaction_type"]) == 2)
        return Decimal(credits), Decimal(debits)

    def iter_wallet_transactions_for_user(self, uid, batch_size=500):
        yield from sorted(self._txns(uid), key=lambda t: int(t["id"]))

    def user_by_employee_id(self, emp):
        return next((u for u in self.users.values() if str(u.get("emp_id") or "").strip() == emp), None)


def _txn(tid, uid, amount, ttype):
    return {
        "id": tid,
        "user_id": uid,
        "amount": str(amount),
        "balance": "0",
        "transaction_type": ttype,
        "create_date": timezone.now(),
        "description": f"txn {tid}",
    }


@pytest.mark.django_db
class TestAdminLegacyUserSync(TestCase):
    def setUp(self):
        # Login sync assertions below need the faculty wallet sync window open.
        PortalMigrationState.objects.update_or_create(
            singleton_key="default",
            defaults={"faculty_wallet_sync_cutoff": timezone.now() + timedelta(days=30)},
        )
        self.iic = Department.objects.create(name=IIC_DEPARTMENT_NAME, department_type=DepartmentType.INTERNAL)
        self.admin = UserFactory(email="main.admin@iitr.ac.in", user_type=UserType.ADMIN)
        self.faculty = UserFactory(
            email="prof.legacy@iitr.ac.in", user_type=UserType.FACULTY, emp_id="777001", admin_approved=True
        )
        self.reader = SqlFakeReader(
            users=[
                {"id": 21, "emp_id": "777001", "name": "Prof Legacy", "email": "prof.legacy@iitr.ac.in", "password": "x"},
                {"id": 45, "emp_id": "22110045", "name": "Stud Legacy", "email": "stud.legacy@iitr.ac.in"},
            ],
            wallets={
                21: {"id": 5, "user_id": 21, "balance": Decimal("1200.00")},
                45: {"id": 9, "user_id": 45, "balance": Decimal("300.00")},
            },
            transactions=[
                _txn(900, 21, "1500.00", 1),
                _txn(901, 21, "300.00", 2),
                _txn(950, 45, "300.00", 1),
            ],
            bookings=[
                {"id": 7001, "user_id": 45, "equipment_id": 3, "booking_date": datetime(2026, 5, 4, 10, 0),
                 "time_required": 90, "status": "2", "charge": Decimal("450.00"), "is_deleted": 0, "is_active": 1},
                {"id": 7002, "user_id": 45, "equipment_id": 3, "booking_date": datetime(2026, 6, 1, 14, 0),
                 "time_required": 60, "status": "1", "charge": Decimal("300.00"), "is_deleted": 1, "is_active": 0},
            ],
        )

    def _student(self, **kwargs):
        return UserFactory(
            email=kwargs.pop("email", "stud.legacy@iitr.ac.in"),
            user_type=UserType.STUDENT,
            emp_id=kwargs.pop("emp_id", "22110045"),
            admin_approved=True,
            **kwargs,
        )

    def _link(self, student, faculty):
        wallet, _ = Wallet.objects.get_or_create(user=faculty)
        WalletJoinRequest.objects.create(
            student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
        )
        return wallet

    def test_preview_is_read_only_and_shows_balance(self):
        preview = build_sync_preview(self.faculty, 21, reader=self.reader)
        self.assertTrue(preview["ok"])
        self.assertEqual(preview["legacy_wallet"]["balance"], "1200.00")
        self.assertEqual(preview["wallet_sync"]["delta"], "1200.00")
        self.assertEqual(preview["wallet_sync"]["migration_id"], "faculty-wallet:777001")
        self.assertNotIn("password", preview["legacy_user"]["details"])
        self.assertFalse(SubWallet.objects.exists())
        self.assertFalse(LegacyWalletAccountMapping.objects.exists())

    def test_confirm_faculty_credits_iic_and_is_idempotent_with_login_sync(self):
        result = apply_sync(
            self.faculty, 21, actor=self.admin, reader=self.reader, expected_legacy_balance="1200.00"
        )
        self.assertEqual(result["wallet"]["delta"], "1200.00")
        sub = SubWallet.objects.get(wallet__user=self.faculty, department=self.iic)
        self.assertEqual(sub.balance, Decimal("1200.00"))
        self.assertEqual(LegacyWalletLedgerEntry.objects.filter(source_user_id=21).count(), 2)
        mapping = LegacyWalletAccountMapping.objects.get(employee_id="777001")
        self.assertEqual(mapping.new_user_id, self.faculty.pk)

        again = apply_sync(self.faculty, 21, actor=self.admin, reader=self.reader, expected_legacy_balance="1200.00")
        self.assertEqual(again["wallet"]["delta"], "0.00")
        login = sync_faculty_wallet_from_legacy(self.faculty, reader=self.reader)
        self.assertTrue(login["ok"])
        self.assertEqual(login["reconcile"]["delta"], "0.00")
        self.assertEqual(SubWallet.objects.get(pk=sub.pk).balance, Decimal("1200.00"))

    def test_confirm_rejects_changed_balance(self):
        with self.assertRaises(AdminSyncError):
            apply_sync(self.faculty, 21, actor=self.admin, reader=self.reader, expected_legacy_balance="999.00")
        self.assertFalse(SubWallet.objects.exists())

    def test_student_balance_goes_to_linked_faculty_wallet(self):
        student = self._student()
        faculty_wallet = self._link(student, self.faculty)
        preview = build_sync_preview(student, 45, reader=self.reader)
        self.assertEqual(preview["wallet_sync"]["selected_target"], TARGET_LINKED_FACULTY)
        apply_sync(student, 45, actor=self.admin, reader=self.reader, expected_legacy_balance="300.00")
        sub = SubWallet.objects.get(wallet=faculty_wallet, department=self.iic)
        self.assertEqual(sub.balance, Decimal("300.00"))
        txn = SubWalletTransaction.objects.get(sub_wallet=sub)
        self.assertEqual(txn.related_user_id, student.pk)
        self.assertFalse(Wallet.objects.filter(user=student).exists())

    def test_unlinked_student_can_still_sync_bookings(self):
        student = self._student()
        eq = Equipment.objects.create(
            name="Legacy EQ",
            code=f"LEQ{uuid.uuid4().hex[:4].upper()}",
            internal_department=self.iic,
            slot_duration_minutes=60,
            status=EquipmentStatus.ACTIVE,
        )
        start = timezone.now() + timedelta(days=2)
        block = LegacyBookingBlock.objects.create(
            legacy_booking_id=7003, legacy_user_id=45, new_equipment=eq, start_at=start, end_at=start + timedelta(hours=1)
        )
        preview = build_sync_preview(student, 45, reader=self.reader)
        self.assertFalse(preview["wallet_sync"]["can_sync"])
        self.assertEqual(preview["bookings"]["count"], 2)
        self.assertEqual(preview["bookings"]["total_charge"], "450.00")
        with self.assertRaises(AdminSyncError):
            apply_sync(student, 45, actor=self.admin, reader=self.reader, expected_legacy_balance="300.00")

        result = apply_sync(student, 45, actor=self.admin, reader=self.reader, sync_wallet=False, sync_bookings=True)
        self.assertEqual(result["bookings"]["archived_created"], 2)
        self.assertEqual(result["bookings"]["blocks_linked"], 1)
        rec = LegacyBookingHistoryRecord.objects.get(source_booking_id=7001)
        self.assertEqual(rec.payload["new_user_id"], student.pk)
        self.assertEqual(rec.payload["duration_minutes"], 90)
        block.refresh_from_db()
        self.assertEqual(block.resolved_user_id, student.pk)
        again = apply_sync(student, 45, actor=self.admin, reader=self.reader, sync_wallet=False, sync_bookings=True)
        self.assertEqual(again["bookings"]["archived_created"], 0)
        self.assertEqual(LegacyBookingHistoryRecord.objects.count(), 2)

    def test_legacy_user_mapped_elsewhere_is_blocked(self):
        other = UserFactory(email="someone@iitr.ac.in", user_type=UserType.FACULTY, emp_id="999999")
        LegacyWalletAccountMapping.objects.create(employee_id="777001", old_user_id=21, new_user=other)
        preview = build_sync_preview(self.faculty, 21, reader=self.reader)
        self.assertFalse(preview["ok"])
        with self.assertRaises(AdminSyncError):
            apply_sync(self.faculty, 21, actor=self.admin, reader=self.reader, expected_legacy_balance="1200.00")

    def test_login_sync_skips_when_admin_mapped_other_legacy_account(self):
        LegacyWalletAccountMapping.objects.create(
            employee_id="LEGACY-UID:45", old_user_id=45, new_user=self.faculty, migration_batch="admin-map:1"
        )
        result = sync_faculty_wallet_from_legacy(self.faculty, reader=self.reader)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "admin_mapped_other_legacy_user")


@pytest.mark.django_db
class TestAdminLegacyUserSyncApi(TestCase):
    def setUp(self):
        Department.objects.create(name=IIC_DEPARTMENT_NAME, department_type=DepartmentType.INTERNAL)
        self.admin = UserFactory(email="main.admin2@iitr.ac.in", user_type=UserType.ADMIN)
        self.faculty = UserFactory(email="api.prof@iitr.ac.in", user_type=UserType.FACULTY, emp_id="555001")
        self.client = APIClient()

    def test_non_admin_forbidden(self):
        self.client.force_authenticate(self.faculty)
        res = self.client.get("/api/portal-migration/admin/legacy-user-sync/search/?q=api")
        self.assertEqual(res.status_code, 403)

    def test_search_and_preview(self):
        self.client.force_authenticate(self.admin)
        res = self.client.get("/api/portal-migration/admin/legacy-user-sync/search/?q=api.prof")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["results"][0]["id"], self.faculty.pk)

        fake = SqlFakeReader(
            users=[{"id": 8, "emp_id": "555001", "name": "API Prof", "email": "api.prof@iitr.ac.in"}],
            wallets={8: {"id": 1, "user_id": 8, "balance": Decimal("75.50")}},
        )
        with patch("iic_booking.users.api.legacy_user_sync_views.OldMySQLReader", return_value=fake):
            res = self.client.post(
                "/api/portal-migration/admin/legacy-user-sync/preview/",
                {"user_id": self.faculty.pk, "legacy_user_id": 8},
                format="json",
            )
            self.assertEqual(res.status_code, 200)
            self.assertEqual(res.data["legacy_wallet"]["balance"], "75.50")
            res = self.client.post(
                "/api/portal-migration/admin/legacy-user-sync/preview/",
                {"user_id": self.faculty.pk, "legacy_user_id": 404},
                format="json",
            )
            self.assertEqual(res.status_code, 400)
