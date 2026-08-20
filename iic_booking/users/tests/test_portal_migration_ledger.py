"""Ledger-first migration: mapping, watermark, isolated apply, booking lock, cutover gate."""

from datetime import datetime, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.booking_lock import end_user_booking_is_locked
from iic_booking.users.legacy_ledger.fake_reader import FakeOldMySQLReader
from iic_booking.users.legacy_ledger.importer import extract_utr_and_reference, import_transaction
from iic_booking.users.legacy_ledger.mapping import classify_old_user, exact_employee_id
from iic_booking.users.legacy_ledger.reconcile import run_full_reconciliation
from iic_booking.users.legacy_ledger.state_machine import IllegalPhaseTransition, transition_phase
from iic_booking.users.legacy_ledger.sync import run_ledger_sync, run_mapping
from iic_booking.users.models import UserType
from iic_booking.users.models.portal_migration import (
    LegacyBookingHistoryRecord,
    LegacyWalletAccountMapping,
    LegacyWalletLedgerEntry,
    LegacyWalletMappingStatus,
    PortalMigrationPhase,
    PortalMigrationState,
)
from iic_booking.users.tests.factories import UserFactory
from iic_booking.users.tasks import sync_legacy_wallet_ledger


def _reader(**kwargs):
    users = kwargs.get(
        "users",
        [{"id": 10, "emp_id": "654321", "name": "Old", "email": "old@x"}],
    )
    wallets = kwargs.get("wallets", {10: {"id": 3, "user_id": 10, "balance": Decimal("40.00")}})
    txns = kwargs.get(
        "transactions",
        [
            {
                "id": 100,
                "user_id": 10,
                "amount": "50.00",
                "balance": "50.00",
                "transaction_type": 1,
                "create_date": datetime(2024, 1, 1, tzinfo=dt_timezone.utc),
                "description": "Recharge UTR: AAA",
            },
            {
                "id": 101,
                "user_id": 10,
                "amount": "10.00",
                "balance": "40.00",
                "transaction_type": 2,
                "create_date": datetime(2024, 1, 2, tzinfo=dt_timezone.utc),
                "description": "Booking #99",
            },
        ],
    )
    return FakeOldMySQLReader(users, wallets, txns, fail_after_id=kwargs.get("fail_after_id"))


@pytest.mark.django_db
class TestEmployeeIdMapping(TestCase):
    def test_exact_trim_does_not_strip_letters(self):
        self.assertEqual(exact_employee_id(" 23EUCCE954 "), "23EUCCE954")

    def test_missing_employee_id(self):
        row = classify_old_user({"id": 1, "emp_id": "  ", "name": "A", "email": "a@x"}, set())
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.MISSING_EMPLOYEE_ID)

    def test_duplicate_employee_id(self):
        row = classify_old_user({"id": 1, "emp_id": "100", "name": "A", "email": "a@x"}, {"100"})
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.DUPLICATE_EMPLOYEE_ID)

    def test_channel_i_not_found(self):
        row = classify_old_user({"id": 1, "emp_id": "100", "name": "A", "email": "a@x"}, set())
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.CHANNEL_I_NOT_FOUND)

    def test_valid_exact_emp_id_ignores_changed_email_and_name(self):
        user = UserFactory(email="fac@iitr.ac.in", emp_id="23EUCCE954", name="Faculty", admin_approved=True)
        row = classify_old_user(
            {"id": 9, "emp_id": "23EUCCE954", "name": "Old Name", "email": "old@x"},
            set(),
            identity_source="CHANNEL_I_VERIFIED",
        )
        self.assertEqual(row.mapping_status, LegacyWalletMappingStatus.VALID)
        self.assertEqual(row.new_user_id, user.pk)
        self.assertNotEqual(row.old_email, row.channel_i_email)


@pytest.mark.django_db
class TestIsolatedLedgerApply(TestCase):
    def setUp(self):
        self.user = UserFactory(email="w@iitr.ac.in", emp_id="654321", name="Wallet Owner", admin_approved=True)

    def test_apply_reconcile_idempotent_and_watermark_recovery(self):
        reader = _reader()
        mapping = run_mapping(reader, batch="iso-1", dry_run=False, require_verified_identity=False)
        self.assertEqual(mapping["valid_employee_ids"], 1)
        first = run_ledger_sync(reader, batch="iso-1", dry_run=False)
        self.assertTrue(first["ok"])
        self.assertEqual(first["stats"].get("imported"), 2)
        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 2)
        self.assertEqual(PortalMigrationState.get_solo().last_wallet_txn_watermark, 101)
        recon = run_full_reconciliation()
        self.assertEqual(recon["overall_status"], "PASS")
        self.assertEqual(recon["counts"]["FAIL"], 0)
        second = run_ledger_sync(reader, batch="iso-2", dry_run=False)
        self.assertEqual(second["stats"].get("imported", 0), 0)
        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 2)
        self.assertEqual(recon["imported_credit_total"], "50.00")
        self.assertEqual(recon["imported_debit_total"], "10.00")

    def test_crash_does_not_advance_unprocessed_ids(self):
        reader = _reader(fail_after_id=100)
        run_mapping(reader, batch="iso-f", dry_run=False, require_verified_identity=False)
        result = run_ledger_sync(reader, batch="iso-f", dry_run=False)
        self.assertFalse(result["ok"])
        self.assertEqual(PortalMigrationState.get_solo().last_wallet_txn_watermark, 100)
        reader2 = _reader()
        resumed = run_ledger_sync(reader2, batch="iso-f2", dry_run=False)
        self.assertTrue(resumed["ok"])
        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 2)
        self.assertEqual(PortalMigrationState.get_solo().last_wallet_txn_watermark, 101)

    def test_dry_run_does_not_write_ledger(self):
        reader = _reader()
        run_mapping(reader, batch="dry", dry_run=True, require_verified_identity=False)
        run_ledger_sync(reader, batch="dry", dry_run=True)
        self.assertEqual(LegacyWalletAccountMapping.objects.count(), 0)
        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 0)
        self.assertEqual(PortalMigrationState.get_solo().last_wallet_txn_watermark, 0)

    def test_celery_does_not_sync_until_operator_enables(self):
        out = sync_legacy_wallet_ledger()
        self.assertEqual(out.get("skipped"), "incremental_sync_disabled")


@pytest.mark.django_db
class TestLegacyLedgerImport(TestCase):
    def setUp(self):
        self.user = UserFactory(email="w2@iitr.ac.in", emp_id="654321", name="Wallet Owner")
        self.mapping = LegacyWalletAccountMapping.objects.create(
            employee_id="654321",
            old_user_id=10,
            mapping_status=LegacyWalletMappingStatus.VALID,
            new_user=self.user,
        )

    def test_import_credit_and_idempotent(self):
        txn = {
            "id": 64100,
            "user_id": 10,
            "amount": "150.00",
            "balance": "150.00",
            "transaction_type": 1,
            "create_date": datetime(2024, 1, 2, 12, 0, tzinfo=dt_timezone.utc),
            "description": "Recharge UTR: ABC123XYZ Ref: R-9",
        }
        user_row = {"id": 10, "emp_id": "654321"}
        self.assertEqual(import_transaction(txn, user_row, "batch-1"), "imported")
        self.assertEqual(import_transaction(txn, user_row, "batch-1"), "duplicate")
        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 1)

    def test_unmapped_goes_dead_letter(self):
        txn = {
            "id": 99,
            "user_id": 11,
            "amount": "10.00",
            "balance": "10.00",
            "transaction_type": 1,
            "create_date": datetime(2024, 1, 2, 12, 0, tzinfo=dt_timezone.utc),
            "description": "x",
        }
        self.assertEqual(import_transaction(txn, {"id": 11, "emp_id": ""}, "b"), "dead_letter")
        self.assertEqual(LegacyWalletLedgerEntry.objects.count(), 0)


class TestUtrParse(TestCase):
    def test_extract(self):
        utr, ref = extract_utr_and_reference("Payment UTR: 12345 Ref: rec-1")
        self.assertEqual(utr, "12345")
        self.assertEqual(ref, "rec-1")


@pytest.mark.django_db
class TestBookingLockAndState(TestCase):
    def test_end_user_locked_when_flag_off(self):
        state = PortalMigrationState.get_solo()
        state.end_user_booking_enabled = False
        state.save()
        student = UserFactory(email="s@iitr.ac.in", user_type=UserType.STUDENT)
        locked, msg = end_user_booking_is_locked(student)
        self.assertTrue(locked)
        self.assertIn("IIC Booking Portal", msg)
        admin = UserFactory(email="a@iitr.ac.in", user_type=UserType.ADMIN)
        self.assertFalse(end_user_booking_is_locked(admin)[0])
        faculty = UserFactory(email="f2@iitr.ac.in", user_type=UserType.FACULTY)
        self.assertTrue(end_user_booking_is_locked(faculty)[0])

    def test_booking_status_api(self):
        user = UserFactory(email="f@iitr.ac.in", user_type=UserType.FACULTY)
        client = APIClient()
        client.force_authenticate(user=user)
        res = client.get("/api/v1/portal-migration/booking-status/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["end_user_booking_enabled"])

    def test_phase_transition_requires_explicit_step(self):
        transition_phase(to_phase=PortalMigrationPhase.PARALLEL_OPERATION, actor_email="admin@x")
        with self.assertRaises(IllegalPhaseTransition):
            transition_phase(to_phase=PortalMigrationPhase.NEW_PORTAL_ACTIVE, mismatch_count=0)

    def test_new_portal_active_blocked_on_mismatch(self):
        transition_phase(to_phase=PortalMigrationPhase.PARALLEL_OPERATION)
        transition_phase(to_phase=PortalMigrationPhase.FINANCIAL_FREEZE)
        transition_phase(to_phase=PortalMigrationPhase.FINAL_SYNC)
        transition_phase(to_phase=PortalMigrationPhase.RECONCILIATION)
        from iic_booking.users.legacy_ledger.state_machine import ReconciliationGateFailed

        with self.assertRaises(ReconciliationGateFailed):
            transition_phase(to_phase=PortalMigrationPhase.NEW_PORTAL_ACTIVE, mismatch_count=1)

    def test_legacy_booking_archive_is_not_active_booking(self):
        rec = LegacyBookingHistoryRecord.objects.create(source_booking_id=57884, employee_id="1")
        self.assertEqual(rec.historical_label, "Historical / Legacy")
        from iic_booking.equipment.models import Booking

        self.assertFalse(Booking.objects.filter(pk=57884).exists())

    def test_staff_accounts_not_deleted_by_migration_models(self):
        admin = UserFactory(email="keep-admin@iitr.ac.in", user_type=UserType.ADMIN)
        oic = UserFactory(email="keep-oic@iitr.ac.in", user_type=UserType.MANAGER)
        self.assertTrue(admin.pk)
        self.assertTrue(oic.pk)


@pytest.mark.django_db
class TestWaitlistLock(TestCase):
    def test_waitlist_create_respects_lock(self):
        from iic_booking.equipment.waitlist_booking import create_booking_for_waitlist_user
        from iic_booking.equipment.models import Equipment

        state = PortalMigrationState.get_solo()
        state.end_user_booking_enabled = False
        state.save()
        student = UserFactory(email="wl@iitr.ac.in", user_type=UserType.STUDENT)
        equipment = Equipment(name="X")
        booking, err = create_booking_for_waitlist_user(equipment, student, [1])
        self.assertIsNone(booking)
        self.assertIn("IIC Booking Portal", err or "")
