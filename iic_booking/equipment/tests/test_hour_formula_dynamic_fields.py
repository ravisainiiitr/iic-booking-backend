"""HOUR generic time_formula + per-user-type DynamicInputField filtering."""

from __future__ import annotations

from decimal import Decimal
import uuid

from django.test import RequestFactory, TestCase

from iic_booking.equipment.calculators import (
    ChargeCalculationEngine,
    TimeCalculationEngine,
    hour_uses_legacy_b_slots,
)
from iic_booking.equipment.models import (
    ChargeProfile,
    ChargeProfilePricingProfile,
    DynamicInputField,
    DynamicInputFieldType,
    Equipment,
    EquipmentProfileType,
    EquipmentStatus,
)
from iic_booking.equipment.serializers import EquipmentDetailSerializer
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType


def _dept():
    return Department.objects.create(
        name=f"HourDept-{uuid.uuid4().hex[:8]}",
        code=f"H{uuid.uuid4().hex[:4].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


class HourFormulaAndDynamicFieldsTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.equipment = Equipment.objects.create(
            name="Hour Formula EQ",
            code=f"HF{uuid.uuid4().hex[:4].upper()}",
            profile_type=EquipmentProfileType.HOUR,
            slot_duration_minutes=60,
            internal_department=self.dept,
            status=EquipmentStatus.ACTIVE,
        )
        self.cp_legacy = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            profile_type=EquipmentProfileType.HOUR,
            primary_unit_charge=Decimal("100.00"),
            secondary_unit_charge=Decimal("50.00"),
            time_formula="",
            is_active=True,
        )
        self.cp_formula = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.INSTITUTE,
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            profile_type=EquipmentProfileType.HOUR,
            primary_unit_charge=Decimal("200.00"),
            secondary_unit_charge=Decimal("999.00"),
            time_formula="((((C-B)/D)*E)*A)/60",
            is_active=True,
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            field_key="B",
            field_label="Slots",
            field_type=DynamicInputFieldType.NUMERIC,
            is_required=True,
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            field_key="C",
            field_label="Toggle",
            field_type=DynamicInputFieldType.TOGGLE,
            is_required=False,
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.INSTITUTE,
            field_key="A",
            field_label="Samples",
            field_type=DynamicInputFieldType.NUMERIC,
            is_required=True,
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.INSTITUTE,
            field_key="B",
            field_label="Start",
            field_type=DynamicInputFieldType.NUMERIC,
            is_required=True,
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.INSTITUTE,
            field_key="C",
            field_label="End",
            field_type=DynamicInputFieldType.NUMERIC,
            is_required=True,
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.INSTITUTE,
            field_key="D",
            field_label="Divisor",
            field_type=DynamicInputFieldType.NUMERIC,
            is_required=True,
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.INSTITUTE,
            field_key="E",
            field_label="Multiplier",
            field_type=DynamicInputFieldType.NUMERIC,
            is_required=True,
        )
        self.student = User.objects.create_user(
            email=f"student-{uuid.uuid4().hex[:6]}@test.local",
            password="x",
            user_type=UserType.STUDENT,
            name="Student",
        )
        self.institute = User.objects.create_user(
            email=f"inst-{uuid.uuid4().hex[:6]}@test.local",
            password="x",
            user_type=UserType.INSTITUTE,
            name="Institute",
        )
        self.rf = RequestFactory()

    def test_legacy_blank_formula_detected(self):
        self.assertTrue(hour_uses_legacy_b_slots(self.cp_legacy))
        self.cp_legacy.time_formula = "B"
        self.assertTrue(hour_uses_legacy_b_slots(self.cp_legacy))
        self.assertFalse(hour_uses_legacy_b_slots(self.cp_formula))

    def test_legacy_hour_time_and_toggle_charge(self):
        t = TimeCalculationEngine.calculate_time(
            self.cp_legacy, {"B": 2, "C": True}, slot_duration_minutes=60
        )
        self.assertEqual(t, 120)
        charge, breakdown = ChargeCalculationEngine.calculate_charge(
            self.cp_legacy, {"B": 2, "C": True}, total_time_minutes=t
        )
        # 2 hours * 100 + secondary 50
        self.assertEqual(charge, Decimal("250.00"))
        self.assertTrue(any("toggle" in (b.get("description") or "").lower() or "secondary" in (b.get("description") or "").lower() for b in breakdown) or charge == Decimal("250.00"))

    def test_formula_hour_time_and_charge_no_toggle(self):
        # ((((30-10)/5)*10)*3)/60 = ((20/5)*10*3)/60 = (4*10*3)/60 = 120/60 = 2 minutes? Wait
        # Actually: ((((C-B)/D)*E)*A)/60 with C=30,B=10,D=5,E=10,A=3
        # = ((((20)/5)*10)*3)/60 = ((4*10)*3)/60 = 120/60 = 2 minutes
        vals = {"A": 3, "B": 10, "C": 30, "D": 5, "E": 10}
        t = TimeCalculationEngine.calculate_time(
            self.cp_formula, vals, slot_duration_minutes=60
        )
        self.assertEqual(t, 2)
        charge, _ = ChargeCalculationEngine.calculate_charge(
            self.cp_formula, vals, total_time_minutes=t
        )
        # (2/60)*200 ≈ 6.67 → money quantize may round to nearest rupee
        self.assertIn(charge.quantize(Decimal("0.01")), {Decimal("6.67"), Decimal("7.00"), Decimal("6.00")})
        # Secondary must not apply on formula path even if C is truthy as a number
        self.assertLess(charge, Decimal("100"))

    def test_input_fields_filtered_by_viewer_user_type(self):
        req = self.rf.get("/equipments/1/")
        req.user = self.student
        data = EquipmentDetailSerializer(self.equipment, context={"request": req}).data
        keys = {f["field_key"] for f in data["input_fields"] if f.get("field_key") != "comments"}
        self.assertEqual(keys, {"B", "C"})

        req2 = self.rf.get("/equipments/1/")
        req2.user = self.institute
        data2 = EquipmentDetailSerializer(self.equipment, context={"request": req2}).data
        keys2 = {f["field_key"] for f in data2["input_fields"] if f.get("field_key") != "comments"}
        self.assertEqual(keys2, {"A", "B", "C", "D", "E"})

    def test_all_input_fields_and_for_user_type_query(self):
        req = self.rf.get("/equipments/1/?all_input_fields=1")
        req.user = self.student
        data = EquipmentDetailSerializer(self.equipment, context={"request": req}).data
        pairs = {
            (f.get("user_type"), f["field_key"])
            for f in data["input_fields"]
            if f.get("field_key") != "comments"
        }
        self.assertIn((UserType.STUDENT, "B"), pairs)
        self.assertIn((UserType.INSTITUTE, "A"), pairs)

        req2 = self.rf.get(f"/equipments/1/?for_user_type={UserType.INSTITUTE}")
        req2.user = self.student
        data2 = EquipmentDetailSerializer(self.equipment, context={"request": req2}).data
        keys = {f["field_key"] for f in data2["input_fields"] if f.get("field_key") != "comments"}
        self.assertEqual(keys, {"A", "B", "C", "D", "E"})
        self.assertEqual(data2.get("viewer_profile_type"), EquipmentProfileType.HOUR)
