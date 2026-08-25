"""Capacity-split (TG/DTA TIME_BAND_FOLD) unit + API tests."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentStatus
from iic_booking.users.legacy_ledger.booking_bridge import discover_legacy_bookings
from iic_booking.users.legacy_ledger.capacity_split import (
    TIME_BAND_FOLD_MAP,
    apply_time_band_fold,
    resolve_legacy_booking_target,
)
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.portal_migration import (
    LegacyEquipmentCapacitySplit,
    LegacyEquipmentCapacitySplitPolicy,
    LegacyEquipmentCapacitySplitStatus,
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def _user(**kwargs):
    email = kwargs.pop("email", None) or f"admin-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(
        email=email,
        password="test-pass-not-used",
        user_type=kwargs.pop("user_type", UserType.ADMIN),
        is_staff=kwargs.pop("is_staff", True),
        **kwargs,
    )


def _dept():
    return Department.objects.create(
        name=f"SplitDept-{uuid.uuid4().hex[:8]}",
        code=f"S{uuid.uuid4().hex[:4].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


def _equipment(dept, *, name, code):
    return Equipment.objects.create(
        name=name,
        code=code,
        internal_department=dept,
        slot_duration_minutes=135,
        status=EquipmentStatus.ACTIVE,
    )


def _local_dt(*, hour: int, minute: int, day_offset: int = 3) -> datetime:
    base = timezone.localtime(timezone.now()) + timedelta(days=day_offset)
    naive = datetime(base.year, base.month, base.day, hour, minute, 0, 0)
    return timezone.make_aware(naive, timezone.get_current_timezone())


class TimeBandFoldEngineTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq_a = _equipment(self.dept, name="DTA/TGA[A]", code="DTAA")
        self.eq_b = _equipment(self.dept, name="DTA/TGA[B]", code="DTAB")
        self.split = LegacyEquipmentCapacitySplit.objects.create(
            old_equipment_id=9001,
            old_equipment_name="TG/DTA",
            target_a=self.eq_a,
            target_b=self.eq_b,
            policy=LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD,
            status=LegacyEquipmentCapacitySplitStatus.ACTIVE,
        )

    def test_scheme_has_eight_slots(self):
        self.assertEqual(len(TIME_BAND_FOLD_MAP), 8)

    def test_overnight_maps_to_b_daytime(self):
        cases = [
            (0, 0, 9, 0),
            (2, 15, 11, 15),
            (4, 30, 13, 30),
            (6, 45, 15, 45),
        ]
        for oh, om, nh, nm in cases:
            start = _local_dt(hour=oh, minute=om)
            end = start + timedelta(minutes=135)
            result = apply_time_band_fold(self.split, start_at=start, end_at=end)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["band"], "B")
            self.assertEqual(result["new_equipment_id"], self.eq_b.equipment_id)
            parsed = datetime.fromisoformat(result["start_at"])
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            local = timezone.localtime(parsed)
            self.assertEqual((local.hour, local.minute), (nh, nm), msg=f"{oh}:{om}")
            self.assertTrue(result["remapped"])

    def test_daytime_maps_to_a_same_clock(self):
        for h, m in [(9, 0), (11, 15), (13, 30), (15, 45)]:
            start = _local_dt(hour=h, minute=m)
            end = start + timedelta(minutes=135)
            result = apply_time_band_fold(self.split, start_at=start, end_at=end)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["band"], "A")
            self.assertEqual(result["new_equipment_id"], self.eq_a.equipment_id)
            parsed = datetime.fromisoformat(result["start_at"])
            if timezone.is_naive(parsed):
                parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
            local = timezone.localtime(parsed)
            self.assertEqual((local.hour, local.minute), (h, m))
            self.assertFalse(result["remapped"])

    def test_unmapped_slot_needs_review(self):
        start = _local_dt(hour=10, minute=0)
        result = apply_time_band_fold(self.split, start_at=start)
        self.assertFalse(result["ok"])
        self.assertTrue(result["needs_review"])
        self.assertIsNone(result["new_equipment_id"])

    def test_resolve_prefers_active_split_over_one_to_one(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=9001,
            old_equipment_name="TG/DTA",
            new_equipment=self.eq_a,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        start = _local_dt(hour=0, minute=0)
        resolved = resolve_legacy_booking_target(
            old_equipment_id=9001,
            start_at=start,
            end_at=start + timedelta(minutes=135),
        )
        assert resolved is not None
        self.assertEqual(resolved["band"], "B")
        self.assertEqual(resolved["new_equipment_id"], self.eq_b.equipment_id)
        self.assertEqual(resolved["policy"], LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD)


class CapacitySplitDiscoveryTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq_a = _equipment(self.dept, name="DTA/TGA[A]", code="DTAA2")
        self.eq_b = _equipment(self.dept, name="DTA/TGA[B]", code="DTAB2")
        self.split = LegacyEquipmentCapacitySplit.objects.create(
            old_equipment_id=42,
            old_equipment_name="TG/DTA",
            target_a=self.eq_a,
            target_b=self.eq_b,
            policy=LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD,
            status=LegacyEquipmentCapacitySplitStatus.ACTIVE,
        )
        state = PortalMigrationState.get_solo()
        start = timezone.now() - timedelta(days=1)
        end = timezone.now() + timedelta(days=30)
        state.migration_start_at = start
        state.migration_window_end_at = end
        state.save(update_fields=["migration_start_at", "migration_window_end_at"])

    def test_discover_remaps_overnight_to_b(self):
        start = _local_dt(hour=2, minute=15)
        end = start + timedelta(minutes=135)
        discovery = discover_legacy_bookings(
            [
                {
                    "legacy_booking_id": 5001,
                    "old_equipment_id": 42,
                    "start_at": start,
                    "end_at": end,
                    "status": "confirmed",
                    "amount": Decimal("0"),
                }
            ]
        )
        eligible = discovery["eligible"]
        self.assertEqual(len(eligible), 1)
        row = eligible[0]
        self.assertEqual(row["new_equipment_id"], self.eq_b.equipment_id)
        self.assertEqual(row["mapping_status"], "CAPACITY_SPLIT")
        self.assertEqual(row["capacity_split"]["band"], "B")
        parsed = datetime.fromisoformat(row["start_at"])
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        local = timezone.localtime(parsed)
        self.assertEqual((local.hour, local.minute), (11, 15))


@override_settings(ROOT_URLCONF="config.urls")
class CapacitySplitApiTests(TestCase):
    def setUp(self):
        self.admin = _user()
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)
        self.dept = _dept()
        self.eq_a = _equipment(self.dept, name="DTA/TGA[A]", code="API_A")
        self.eq_b = _equipment(self.dept, name="DTA/TGA[B]", code="API_B")
        # Ensure bridge schema looks ready via real tables (migrations applied in tests).
        PortalMigrationState.get_solo()

    def test_create_active_split_supersedes_one_to_one(self):
        mapping = LegacyEquipmentMapping.objects.create(
            old_equipment_id=77,
            old_equipment_name="TG/DTA",
            new_equipment=self.eq_a,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        res = self.client.post(
            "/api/portal-migration/admin/equipment-mappings/capacity-splits/",
            {
                "old_equipment_id": 77,
                "old_equipment_name": "TG/DTA",
                "target_a_id": self.eq_a.equipment_id,
                "target_b_id": self.eq_b.equipment_id,
                "policy": "TIME_BAND_FOLD",
                "status": "ACTIVE",
            },
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data.get("target_a_id"), self.eq_a.equipment_id)
        mapping.refresh_from_db()
        self.assertEqual(mapping.status, LegacyEquipmentMappingStatus.DISABLED)
        self.assertGreaterEqual(res.data.get("superseded_one_to_one_mappings", 0), 1)

    def test_preview_endpoint(self):
        split = LegacyEquipmentCapacitySplit.objects.create(
            old_equipment_id=88,
            target_a=self.eq_a,
            target_b=self.eq_b,
            status=LegacyEquipmentCapacitySplitStatus.ACTIVE,
            policy=LegacyEquipmentCapacitySplitPolicy.TIME_BAND_FOLD,
        )
        start = _local_dt(hour=0, minute=0)
        res = self.client.post(
            f"/api/portal-migration/admin/equipment-mappings/capacity-splits/{split.id}/preview/",
            {
                "rows": [
                    {
                        "legacy_booking_id": 1,
                        "old_equipment_id": 88,
                        "start_at": start.isoformat(),
                        "end_at": (start + timedelta(minutes=135)).isoformat(),
                        "status": "confirmed",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.data["counts"]["band_b"], 1)
        self.assertEqual(res.data["counts"]["assigned"], 1)
