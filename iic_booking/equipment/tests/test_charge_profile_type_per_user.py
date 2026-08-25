"""Per-user-type ChargeProfile.profile_type (mixed modes on one equipment)."""

from __future__ import annotations

from decimal import Decimal
import uuid

from django.test import TestCase

from iic_booking.equipment.calculators import (
    ChargeCalculationEngine,
    TimeCalculationEngine,
    get_charge_profile_type,
)
from iic_booking.equipment.models import (
    ChargeProfile,
    ChargeProfilePricingProfile,
    Equipment,
    EquipmentProfileType,
    EquipmentStatus,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType


def _dept():
    return Department.objects.create(
        name=f"CPDept-{uuid.uuid4().hex[:8]}",
        code=f"C{uuid.uuid4().hex[:4].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


class MixedChargeProfileTypeTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.equipment = Equipment.objects.create(
            name="Mixed Profile EQ",
            code=f"MX{uuid.uuid4().hex[:4].upper()}",
            profile_type=EquipmentProfileType.SAMPLE,  # legacy default
            slot_duration_minutes=60,
            internal_department=self.dept,
            status=EquipmentStatus.ACTIVE,
        )
        self.cp_sample = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            profile_type=EquipmentProfileType.SAMPLE,
            primary_unit_charge=Decimal("100.00"),
            secondary_unit_charge=Decimal("0.00"),
            time_formula="A * 60",
            is_active=True,
        )
        self.cp_hour = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.INSTITUTE,
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            profile_type=EquipmentProfileType.HOUR,
            primary_unit_charge=Decimal("500.00"),
            secondary_unit_charge=Decimal("0.00"),
            is_active=True,
        )

    def test_effective_types_differ_by_user_type(self):
        self.assertEqual(get_charge_profile_type(self.cp_sample), EquipmentProfileType.SAMPLE)
        self.assertEqual(get_charge_profile_type(self.cp_hour), EquipmentProfileType.HOUR)
        self.assertEqual(self.cp_sample.effective_profile_type, EquipmentProfileType.SAMPLE)
        self.assertEqual(self.cp_hour.effective_profile_type, EquipmentProfileType.HOUR)

    def test_time_calc_follows_charge_profile_not_equipment(self):
        # SAMPLE uses formula A*60 → 120 minutes for A=2
        t_sample = TimeCalculationEngine.calculate_time(
            self.cp_sample, {"A": 2}, slot_duration_minutes=60
        )
        self.assertEqual(t_sample, 120)
        # HOUR uses B as slots → B=3 → 180 minutes
        t_hour = TimeCalculationEngine.calculate_time(
            self.cp_hour, {"B": 3}, slot_duration_minutes=60
        )
        self.assertEqual(t_hour, 180)

    def test_fallback_to_equipment_when_cp_type_empty(self):
        self.cp_sample.profile_type = None
        self.cp_sample.save(update_fields=["profile_type"])
        self.assertEqual(get_charge_profile_type(self.cp_sample), EquipmentProfileType.SAMPLE)

    def test_charge_calc_uses_cp_type(self):
        total, _ = ChargeCalculationEngine.calculate_charge(
            self.cp_hour, {"B": 2}, total_time_minutes=120
        )
        # HOUR: 2 hours * 500
        self.assertEqual(total, Decimal("1000"))
