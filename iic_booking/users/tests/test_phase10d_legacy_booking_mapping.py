"""Phase 10D — legacy equipment + booking slot mapping (user mapping decoupled from blocks)."""

from __future__ import annotations

from datetime import timedelta
import uuid

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
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
)
from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mapping_save
from iic_booking.users.legacy_ledger.legacy_booking_mysql import (
    build_t0_dataset_summary,
    map_legacy_identities,
)
from iic_booking.users.legacy_ledger.legacy_user_resolution import (
    classify_user_mapping_for_row,
    lookup_new_portal_user_by_employee_id,
    resolve_legacy_blocks_for_channel_i_user,
)
from iic_booking.users.legacy_ledger.migration_dry_run import migration_dry_run
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    LegacyBookingMigrationBatch,
    LegacyBookingMigrationBatchStatus,
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
    LegacyUserMappingStatus,
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
        name=f"10DDept-{uuid.uuid4().hex[:8]}",
        code=code or f"D{uuid.uuid4().hex[:4].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


def _equipment(dept, **kwargs):
    return Equipment.objects.create(
        name=kwargs.pop("name", "10D EQ"),
        code=kwargs.pop("code", f"10D{uuid.uuid4().hex[:4].upper()}"),
        internal_department=dept,
        slot_duration_minutes=60,
        status=EquipmentStatus.ACTIVE,
        **kwargs,
    )


def _slot(equipment, start=None, end=None, status=SlotStatus.AVAILABLE):
    start = start or (timezone.now() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
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


def _fixture_row(**overrides):
    start = timezone.now() + timedelta(days=5)
    end = start + timedelta(hours=2)
    base = {
        "legacy_booking_id": int(uuid.uuid4().int % 900000) + 1000,
        "old_equipment_id": 101,
        "start_at": start,
        "end_at": end,
        "status": "CONFIRMED",
        "legacy_user_id": 42,
        "employee_id": "EMP10042",
        "duration_minutes": 120,
    }
    base.update(overrides)
    return base


class Phase10DEquipmentMappingTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.admin = _user(UserType.ADMIN)
        self.client = APIClient()

    def test_equipment_mapping_crud(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.post(
            "/api/portal-migration/admin/equipment-mappings/",
            {
                "old_equipment_id": 501,
                "old_equipment_name": "Legacy SEM",
                "new_equipment_id": self.eq.equipment_id,
                "status": "ACTIVE",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        mid = res.data["id"]
        res = self.client.patch(
            f"/api/portal-migration/admin/equipment-mappings/{mid}/",
            {"mapping_reason": "operator confirmed"},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["mapping_reason"], "operator confirmed")

    def test_validate_duplicate_legacy_mapping(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=888,
            new_equipment=self.eq,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        v = validate_legacy_equipment_mapping_save(
            old_equipment_id=888,
            new_equipment_id=self.eq.equipment_id,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.assertFalse(v["valid"])
        self.assertIn("duplicate_legacy_equipment_mapping", v["errors"])

    def test_many_to_one_warning_not_silent(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=701,
            new_equipment=self.eq,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        v = validate_legacy_equipment_mapping_save(
            old_equipment_id=702,
            new_equipment_id=self.eq.equipment_id,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.assertTrue(v["valid"])
        self.assertTrue(any("multiple legacy equipment" in w for w in v["warnings"]))


class Phase10DScopeTests(TestCase):
    def setUp(self):
        self.dept_a = _dept()
        self.dept_b = _dept()
        self.eq_a = _equipment(self.dept_a)
        self.eq_b = _equipment(self.dept_b)
        self.admin = _user(UserType.ADMIN)
        self.oic_a = _user(UserType.MANAGER)
        EquipmentManager.objects.create(equipment=self.eq_a, manager=self.oic_a)
        self.student = _user(UserType.STUDENT)
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=201,
            new_equipment=self.eq_a,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=202,
            new_equipment=self.eq_b,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.client = APIClient()
        state = PortalMigrationState.get_solo()
        state.migration_start_at = timezone.now()
        state.migration_window_end_at = timezone.now() + timedelta(days=30)
        state.save()

    def test_main_admin_global_scope(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/legacy-bookings/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["scope"], "global")

    def test_oic_scoped_legacy_bookings(self):
        row_a = _fixture_row(old_equipment_id=201, legacy_booking_id=3001)
        row_b = _fixture_row(old_equipment_id=202, legacy_booking_id=3002)
        self.client.force_authenticate(user=self.oic_a)
        res = self.client.post(
            "/api/portal-migration/admin/legacy-bookings/",
            {"legacy_rows": [row_a, row_b]},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["scope"], "oic_equipment")
        ids = {r["legacy_booking_id"] for r in res.data["results"]}
        self.assertIn(3001, ids)
        self.assertNotIn(3002, ids)

    def test_oic_cannot_modify_equipment_mappings(self):
        self.client.force_authenticate(user=self.oic_a)
        res = self.client.post(
            "/api/portal-migration/admin/equipment-mappings/",
            {"old_equipment_id": 999, "new_equipment_id": self.eq_a.equipment_id, "status": "ACTIVE"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

    def test_normal_user_cannot_modify_mappings(self):
        self.client.force_authenticate(user=self.student)
        res = self.client.get("/api/portal-migration/admin/equipment-mappings/")
        self.assertEqual(res.status_code, 403)


class Phase10DDiscoveryTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=101,
            new_equipment=self.eq,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        state = PortalMigrationState.get_solo()
        state.migration_start_at = timezone.now() - timedelta(days=1)
        state.migration_window_end_at = timezone.now() + timedelta(days=30)
        state.save()

    def test_legacy_booking_discovery_and_emp_id(self):
        row = _fixture_row(employee_id="EMP55555", legacy_user_id=99)
        disc = discover_legacy_bookings([row])
        self.assertEqual(disc["counts"]["eligible"], 1)
        entry = disc["eligible"][0]
        self.assertEqual(entry["legacy_user_id"], 99)
        self.assertEqual(entry["legacy_employee_id"], "EMP55555")

    def test_unresolved_user_does_not_block_slot_blocking(self):
        row = _fixture_row(employee_id="NO_SUCH_EMP", legacy_user_id=77)
        disc = discover_legacy_bookings([row])
        self.assertEqual(disc["counts"]["eligible"], 1)
        self.assertEqual(disc["eligible"][0]["user_mapping_status"], LegacyUserMappingStatus.UNRESOLVED)

    def test_unmapped_equipment_not_eligible(self):
        row = _fixture_row(old_equipment_id=9999)
        disc = discover_legacy_bookings([row])
        self.assertEqual(disc["counts"]["unmapped"], 1)

    def test_cancelled_does_not_block(self):
        row = _fixture_row(status="CANCELLED")
        disc = discover_legacy_bookings([row])
        self.assertEqual(disc["counts"]["cancelled"], 1)
        self.assertEqual(disc["counts"]["eligible"], 0)

    def test_completed_outside_window_invalid(self):
        past = timezone.now() - timedelta(days=10)
        row = _fixture_row(start_at=past, end_at=past + timedelta(hours=1), status="COMPLETED")
        disc = discover_legacy_bookings([row])
        self.assertEqual(disc["counts"]["completed"], 1)
        future = timezone.now() + timedelta(days=400)
        row2 = _fixture_row(start_at=future, end_at=future + timedelta(hours=1))
        disc2 = discover_legacy_bookings([row2])
        self.assertEqual(disc2["counts"]["outside_window"], 1)

    def test_duplicate_legacy_booking_bucket(self):
        row = _fixture_row(legacy_booking_id=5555)
        disc = discover_legacy_bookings([row, dict(row)])
        self.assertEqual(disc["counts"]["duplicate"], 1)
        self.assertEqual(disc["counts"]["eligible"], 1)


class Phase10DSlotBlockingTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.start = (timezone.now() + timedelta(days=4)).replace(minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)
        self.slot = _slot(self.eq, start=self.start, end=self.end)

    def test_mapped_equipment_unresolved_user_creates_block(self):
        block = arm_legacy_block(
            legacy_booking_id=88001,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
            legacy_user_id=12,
            legacy_employee_id="UNRESOLVED_EMP",
            legacy_equipment_id=101,
            duration_minutes=60,
            source_status="CONFIRMED",
            user_mapping_status=LegacyUserMappingStatus.UNRESOLVED,
        )
        self.assertEqual(block.status, LegacyBookingBlockStatus.ACTIVE)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, SlotStatus.BLOCKED)
        self.assertTrue((self.slot.blocked_label or "").startswith("LEGACY_MIGRATION:"))

    def test_duplicate_block_guard(self):
        arm_legacy_block(
            legacy_booking_id=88002,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
        )
        with self.assertRaises(ValueError):
            arm_legacy_block(
                legacy_booking_id=88002,
                equipment=self.eq,
                start_at=self.start,
                end_at=self.end,
            )

    def test_invalid_datetime_no_block(self):
        before = LegacyBookingBlock.objects.count()
        disc = discover_legacy_bookings([{"legacy_booking_id": 1, "old_equipment_id": 101, "status": "OK"}])
        self.assertEqual(disc["counts"]["invalid"], 1)
        self.assertEqual(LegacyBookingBlock.objects.count(), before)

    def test_abort_releases_blocks_without_mysql(self):
        batch = LegacyBookingMigrationBatch.objects.create(
            window_start=self.start,
            window_end=self.end + timedelta(days=1),
            status=LegacyBookingMigrationBatchStatus.DRAFT,
        )
        block = arm_legacy_block(
            legacy_booking_id=88003,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
            batch=batch,
        )
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, SlotStatus.BLOCKED)
        result = abort_migration_batch(batch, reason="test_abort")
        self.assertGreaterEqual(result["released"], 1)
        block.refresh_from_db()
        self.assertEqual(block.status, LegacyBookingBlockStatus.RELEASED)
        self.slot.refresh_from_db()
        self.assertEqual(self.slot.status, SlotStatus.AVAILABLE)

    def test_overlapping_new_booking_returns_409(self):
        from iic_booking.users.legacy_ledger.booking_bridge import slots_blocked_by_legacy_migration

        arm_legacy_block(
            legacy_booking_id=88004,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
        )
        blocked = slots_blocked_by_legacy_migration([self.slot.id])
        self.assertTrue(blocked)


class Phase10DUserResolutionTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.start = (timezone.now() + timedelta(days=6)).replace(minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)
        _slot(self.eq, start=self.start, end=self.end)

    def test_channel_i_login_resolves_legacy_user(self):
        user = _user(UserType.FACULTY, emp_id="CI100001")
        block = arm_legacy_block(
            legacy_booking_id=99001,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
            legacy_employee_id="CI100001",
            user_mapping_status=LegacyUserMappingStatus.UNRESOLVED,
        )
        result = resolve_legacy_blocks_for_channel_i_user(user)
        self.assertEqual(result["updated"], 1)
        block.refresh_from_db()
        self.assertEqual(block.user_mapping_status, LegacyUserMappingStatus.RESOLVED_CHANNEL_I)
        self.assertEqual(block.resolved_user_id, user.pk)
        self.assertEqual(block.status, LegacyBookingBlockStatus.ACTIVE)

    def test_wrong_employee_id_does_not_resolve(self):
        user = _user(UserType.FACULTY, emp_id="WRONG999")
        arm_legacy_block(
            legacy_booking_id=99002,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
            legacy_employee_id="CI100002",
            user_mapping_status=LegacyUserMappingStatus.UNRESOLVED,
        )
        result = resolve_legacy_blocks_for_channel_i_user(user)
        self.assertEqual(result["updated"], 0)

    def test_email_name_cannot_resolve_identity(self):
        _user(UserType.STUDENT, emp_id="", email="emp777@test.local", name="EMP777")
        by_emp2, reason = lookup_new_portal_user_by_employee_id("")
        self.assertIsNone(by_emp2)
        self.assertEqual(reason, "missing_employee_id")
        row = classify_user_mapping_for_row(legacy_employee_id="", legacy_user_id=1)
        self.assertEqual(row["user_mapping_status"], LegacyUserMappingStatus.UNRESOLVED)

    def test_map_legacy_identities_unresolved_not_blocker(self):
        row = {
            "legacy_booking_id": 1,
            "legacy_user_id": 5,
            "employee_id": "UNKNOWN_EMP_10D",
            "old_equipment_id": 101,
            "start_at": timezone.now().isoformat(),
            "end_at": (timezone.now() + timedelta(hours=1)).isoformat(),
        }
        report = map_legacy_identities([row])
        self.assertTrue(report.get("ok"))
        self.assertFalse(report.get("user_mapping_blocks_readiness"))
        self.assertGreaterEqual(report.get("unresolved_count", 0), 1)

    def test_t0_summary_unresolved_not_blocker(self):
        summary = build_t0_dataset_summary(discovery_counts={"eligible": 3}, identity_exceptions=5)
        self.assertNotIn("unresolved_identities", summary["blockers"])
        self.assertFalse(summary["user_mapping_blocks_readiness"])


class Phase10DDryRunTests(TestCase):
    def test_dry_run_zero_blocks_and_user_counts(self):
        before = LegacyBookingBlock.objects.count()
        row = _fixture_row(employee_id="DRY001")
        report = migration_dry_run([row])
        self.assertEqual(LegacyBookingBlock.objects.count(), before)
        self.assertIn("user_identity_unresolved", report)
        self.assertFalse(report["user_mapping_blocks_readiness"])
