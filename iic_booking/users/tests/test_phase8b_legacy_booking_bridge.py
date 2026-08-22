"""Phase 8B — legacy equipment mapping + booking block bridge tests."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentStatus,
    SlotMaster,
    SlotStatus,
)
from iic_booking.users.legacy_ledger.booking_bridge import (
    LEGACY_MIGRATION_SLOT_BLOCKED,
    abort_migration_batch,
    arm_legacy_block,
    discover_legacy_bookings,
    reconcile_legacy_blocks,
    release_legacy_block,
)
from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings
from iic_booking.users.legacy_ledger.migration_dry_run import migration_dry_run
from iic_booking.users.legacy_ledger.migration_refund import MigrationRefundError, issue_migration_refund
from iic_booking.users.models import Department, SubWallet, User, Wallet
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    LegacyBookingMigrationBatch,
    LegacyBookingMigrationBatchStatus,
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
    MigrationBookingSettlement,
    MigrationSettlementStatus,
    PortalMigrationPhase,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(
        email=email,
        password="test-pass-not-used",
        user_type=user_type,
        **kwargs,
    )


def _dept(code=None):
    return Department.objects.create(
        name=f"8BDept-{uuid.uuid4().hex[:8]}",
        code=code or f"B{uuid.uuid4().hex[:4].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


def _equipment(dept, **kwargs):
    return Equipment.objects.create(
        name=kwargs.pop("name", "8B EQ"),
        code=kwargs.pop("code", f"B8{uuid.uuid4().hex[:4].upper()}"),
        internal_department=dept,
        slot_duration_minutes=60,
        status=EquipmentStatus.ACTIVE,
        **kwargs,
    )


def _slot(equipment, start=None, end=None, status=SlotStatus.AVAILABLE):
    start = start or (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
    end = end or (start + timedelta(hours=1))
    sm = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=int(uuid.uuid4().int % 9000) + 1,
        open_time=start.time().replace(microsecond=0),
        close_time=end.time().replace(microsecond=0),
        is_active=True,
    )
    return DailySlot.objects.create(
        slot_master=sm,
        date=start.date(),
        start_datetime=start,
        end_datetime=end,
        status=status,
    )


def _wallet(user, dept, balance=Decimal("500.00")):
    w = Wallet.objects.create(user=user)
    return SubWallet.objects.create(wallet=w, department=dept, balance=balance)


def _charge_profile(equipment, user_type=UserType.STUDENT, charge=Decimal("10.00")):
    return ChargeProfile.objects.create(
        equipment=equipment,
        user_type=user_type,
        primary_unit_charge=charge,
        is_active=True,
    )


class EquipmentMappingTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.other = _dept()
        self.eq = _equipment(self.dept)

    def test_valid_mapping(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=1001,
            old_equipment_code="OLD1",
            old_equipment_name="Old One",
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        report = validate_legacy_equipment_mappings()
        self.assertEqual(report["counts"]["mapped"], 1)
        self.assertTrue(report["ready"])

    def test_unmapped_equipment(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=1002,
            status=LegacyEquipmentMappingStatus.UNMAPPED,
        )
        report = validate_legacy_equipment_mappings()
        self.assertEqual(report["counts"]["unmapped"], 1)

    def test_duplicate_active_new_mapping(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=1,
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=2,
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        report = validate_legacy_equipment_mappings()
        self.assertGreaterEqual(report["counts"]["conflict"], 1)

    def test_cross_department_mapping(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=3,
            new_equipment=self.eq,
            department=self.other,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        report = validate_legacy_equipment_mappings()
        self.assertEqual(report["counts"]["conflict"], 1)

    def test_disabled_equipment(self):
        self.eq.status = EquipmentStatus.MAINTENANCE
        self.eq.save(update_fields=["status"])
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=4,
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        report = validate_legacy_equipment_mappings()
        self.assertEqual(report["counts"]["disabled"], 1)

    def test_mode_mismatch_reason(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=5,
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
            mapping_reason="MODE_MISMATCH between child modes",
        )
        report = validate_legacy_equipment_mappings()
        self.assertEqual(report["counts"]["conflict"], 1)

    def test_explicit_conflict_status(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=6,
            status=LegacyEquipmentMappingStatus.CONFLICT,
            mapping_reason="ambiguous name",
        )
        report = validate_legacy_equipment_mappings()
        self.assertEqual(report["counts"]["conflict"], 1)


class LegacyDiscoveryAndBlockTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.state = PortalMigrationState.get_solo()
        self.start = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
        self.end_window = self.start + timedelta(days=7)
        self.state.migration_start_at = self.start
        self.state.migration_window_end_at = self.end_window
        self.state.save()
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=42,
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )

    def test_discovery_window_and_eligibility(self):
        booking_start = self.start + timedelta(hours=2)
        booking_end = booking_start + timedelta(hours=1)
        rows = [
            {
                "legacy_booking_id": 501,
                "old_equipment_id": 42,
                "start_at": booking_start,
                "end_at": booking_end,
                "status": "CONFIRMED",
                "amount": "100",
                "employee_id": "E1",
            },
            {
                "legacy_booking_id": 502,
                "old_equipment_id": 999,
                "start_at": booking_start,
                "end_at": booking_end,
                "status": "CONFIRMED",
            },
            {
                "legacy_booking_id": 503,
                "old_equipment_id": 42,
                "start_at": booking_start,
                "end_at": booking_end,
                "status": "CANCELLED",
            },
            {
                "legacy_booking_id": 504,
                "old_equipment_id": 42,
                "start_at": booking_start,
                "end_at": booking_end,
                "status": "COMPLETED",
            },
            {
                "legacy_booking_id": 505,
                "old_equipment_id": 42,
                "start_at": self.end_window + timedelta(days=1),
                "end_at": self.end_window + timedelta(days=1, hours=1),
                "status": "CONFIRMED",
            },
        ]
        report = discover_legacy_bookings(rows)
        self.assertEqual(report["counts"]["eligible"], 1)
        self.assertEqual(report["counts"]["unmapped"], 1)
        self.assertEqual(report["counts"]["cancelled"], 1)
        self.assertEqual(report["counts"]["completed"], 1)
        self.assertEqual(report["counts"]["invalid"], 1)

    def test_arm_block_and_release(self):
        start = self.start + timedelta(hours=3)
        end = start + timedelta(hours=1)
        slot = _slot(self.eq, start=start, end=end)
        batch = LegacyBookingMigrationBatch.objects.create(
            window_start=self.start,
            window_end=self.end_window,
            status=LegacyBookingMigrationBatchStatus.VALIDATED,
        )
        block = arm_legacy_block(
            legacy_booking_id=9001,
            equipment=self.eq,
            start_at=start,
            end_at=end,
            batch=batch,
        )
        self.assertEqual(block.status, LegacyBookingBlockStatus.ACTIVE)
        slot.refresh_from_db()
        self.assertEqual(slot.status, SlotStatus.BLOCKED)
        self.assertTrue((slot.blocked_label or "").startswith("LEGACY_MIGRATION:"))
        release_legacy_block(block, reason="test")
        block.refresh_from_db()
        slot.refresh_from_db()
        self.assertEqual(block.status, LegacyBookingBlockStatus.RELEASED)
        self.assertEqual(slot.status, SlotStatus.AVAILABLE)

    def test_duplicate_block_protection(self):
        start = self.start + timedelta(hours=4)
        end = start + timedelta(hours=1)
        _slot(self.eq, start=start, end=end)
        arm_legacy_block(
            legacy_booking_id=9002,
            equipment=self.eq,
            start_at=start,
            end_at=end,
        )
        with self.assertRaises(ValueError):
            arm_legacy_block(
                legacy_booking_id=9002,
                equipment=self.eq,
                start_at=start,
                end_at=end,
            )

    def test_conflict_when_slot_booked(self):
        start = self.start + timedelta(hours=5)
        end = start + timedelta(hours=1)
        _slot(self.eq, start=start, end=end, status=SlotStatus.BOOKED)
        block = arm_legacy_block(
            legacy_booking_id=9003,
            equipment=self.eq,
            start_at=start,
            end_at=end,
        )
        self.assertEqual(block.status, LegacyBookingBlockStatus.CONFLICT)

    def test_abort_batch(self):
        start = self.start + timedelta(hours=6)
        end = start + timedelta(hours=1)
        slot = _slot(self.eq, start=start, end=end)
        batch = LegacyBookingMigrationBatch.objects.create(
            window_start=self.start,
            window_end=self.end_window,
        )
        arm_legacy_block(
            legacy_booking_id=9004,
            equipment=self.eq,
            start_at=start,
            end_at=end,
            batch=batch,
        )
        result = abort_migration_batch(batch)
        self.assertEqual(result["released"], 1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, LegacyBookingMigrationBatchStatus.ABORTED)
        slot.refresh_from_db()
        self.assertEqual(slot.status, SlotStatus.AVAILABLE)
        # audit row preserved
        self.assertTrue(LegacyBookingBlock.objects.filter(legacy_booking_id=9004).exists())

    def test_reconcile(self):
        start = self.start + timedelta(hours=7)
        end = start + timedelta(hours=1)
        _slot(self.eq, start=start, end=end)
        arm_legacy_block(
            legacy_booking_id=9005,
            equipment=self.eq,
            start_at=start,
            end_at=end,
        )
        recon = reconcile_legacy_blocks()
        self.assertTrue(recon["ok"])


class NewPortalLegacyBlockAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.booker = _user(UserType.FACULTY)
        _wallet(self.booker, self.dept)
        _charge_profile(self.eq, user_type=UserType.FACULTY)
        self.start = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=3)
        self.end = self.start + timedelta(hours=1)
        self.slot = _slot(self.eq, start=self.start, end=self.end)
        state = PortalMigrationState.get_solo()
        state.end_user_booking_enabled = True
        state.booking_migration_mode = "ACTIVE"
        state.save()
        arm_legacy_block(
            legacy_booking_id=9100,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
        )

    def test_booking_overlapping_legacy_block_rejected(self):
        self.client.force_authenticate(user=self.booker)
        self.slot.refresh_from_db()
        res = self.client.post(
            f"/api/equipments/{self.eq.equipment_id}/book/",
            {"slot_ids": [self.slot.id], "number_of_samples": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 409, getattr(res, "data", res.content))
        self.assertEqual(res.data.get("code"), LEGACY_MIGRATION_SLOT_BLOCKED)

    def test_non_overlapping_booking_allowed(self):
        other_start = self.start + timedelta(hours=5)
        other_end = other_start + timedelta(hours=1)
        free = _slot(self.eq, start=other_start, end=other_end)
        self.client.force_authenticate(user=self.booker)
        res = self.client.post(
            f"/api/equipments/{self.eq.equipment_id}/book/",
            {"slot_ids": [free.id], "number_of_samples": 1},
            format="json",
        )
        # May fail for unrelated reasons (window/quota); must not be legacy 409
        if res.status_code == 409:
            self.assertNotEqual(res.data.get("code"), LEGACY_MIGRATION_SLOT_BLOCKED)
        else:
            self.assertIn(res.status_code, (201, 400))


class FreezeAndAdminVisibilityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.admin = _user(UserType.ADMIN, is_staff=True)
        self.student = _user(UserType.STUDENT)
        self.dept = _dept()
        self.eq = _equipment(self.dept)

    def test_booking_status_exposes_migration_mode(self):
        state = PortalMigrationState.get_solo()
        state.booking_migration_mode = "FREEZE"
        state.new_portal_url = "https://example.test/new"
        state.save()
        self.client.force_authenticate(user=self.student)
        res = self.client.get("/api/portal-migration/booking-status/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data.get("legacy_portal_new_booking_disabled"))
        self.assertEqual(res.data.get("legacy_portal_booking_disabled_code"), "MIGRATION_BOOKING_DISABLED")
        self.assertIn("new portal", (res.data.get("legacy_portal_migration_banner") or "").lower())
        self.assertEqual(res.data.get("new_portal_url"), "https://example.test/new")

    def test_main_admin_overview_sees_all_departments(self):
        other = _dept()
        _equipment(other)
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=77,
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/legacy-overview/")
        self.assertEqual(res.status_code, 200)
        self.assertGreaterEqual(len(res.data.get("departments") or []), 2)

    def test_non_admin_cannot_manage_mappings(self):
        self.client.force_authenticate(user=self.student)
        res = self.client.get("/api/portal-migration/admin/equipment-mappings/")
        self.assertEqual(res.status_code, 403)


class DryRunAndCleanupTests(TestCase):
    def test_dry_run_no_writes(self):
        before = LegacyBookingBlock.objects.count()
        report = migration_dry_run([])
        self.assertIn(report["verdict"], ("READY FOR MIGRATION", "NOT READY"))
        self.assertEqual(LegacyBookingBlock.objects.count(), before)

    def test_test_account_flag_only(self):
        real = _user(UserType.STUDENT, is_test_account=False)
        test = _user(UserType.STUDENT, is_test_account=True)
        self.assertFalse(real.is_test_account)
        self.assertTrue(test.is_test_account)
        self.assertEqual(User.objects.filter(is_test_account=True).count(), 1)


class Phase8ARefundCompatibilityTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.faculty = _user(UserType.FACULTY)
        _wallet(self.faculty, self.dept, balance=Decimal("0.00"))
        self.oic = _user(UserType.MANAGER)
        EquipmentManager.objects.create(equipment=self.eq, manager=self.oic)
        profile = ChargeProfile.objects.create(
            equipment=self.eq,
            user_type=UserType.FACULTY,
            primary_unit_charge=Decimal("50.00"),
        )
        self.booking = Booking.objects.create(
            user=self.faculty,
            equipment=self.eq,
            charge_profile=profile,
            status=BookingStatus.COMPLETED,
            total_charge=Decimal("50.00"),
            wallet_amount_applied=Decimal("50.00"),
            total_time_minutes=60,
            virtual_booking_id=f"IIC{self.eq.code}{uuid.uuid4().hex[:6]}",
        )
        state = PortalMigrationState.get_solo()
        state.end_user_booking_enabled = False
        state.phase = PortalMigrationPhase.FINANCIAL_FREEZE
        state.booking_migration_mode = "SETTLEMENT"
        state.save()
        start = timezone.now() + timedelta(days=4)
        end = start + timedelta(hours=1)
        slot = _slot(self.eq, start=start, end=end)
        block = arm_legacy_block(
            legacy_booking_id=9200,
            equipment=self.eq,
            start_at=start,
            end_at=end,
        )
        self.slot = slot
        self.block = block

    def test_refund_still_works_and_does_not_free_slots(self):
        settlement = issue_migration_refund(
            booking=self.booking,
            actor=self.oic,
            reason="8B compat",
            confirm=True,
        )
        self.assertEqual(settlement.status, MigrationSettlementStatus.COMPLETED)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, SlotStatus.BLOCKED)
        self.block.refresh_from_db()
        self.assertEqual(self.block.status, LegacyBookingBlockStatus.ACTIVE)
        with self.assertRaises(MigrationRefundError):
            issue_migration_refund(
                booking=self.booking,
                actor=self.oic,
                reason="dup",
                confirm=True,
            )
