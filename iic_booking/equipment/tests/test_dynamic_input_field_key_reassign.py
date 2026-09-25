"""Admin inline: dynamic input field keys can be reassigned in a single save."""

from __future__ import annotations

import uuid

from django.contrib import admin
from django.test import RequestFactory, TestCase

from iic_booking.equipment.admin import DynamicInputFieldInline
from iic_booking.equipment.models import (
    DynamicInputField,
    DynamicInputFieldType,
    Equipment,
    EquipmentProfileType,
    EquipmentStatus,
)
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType


class DynamicInputFieldKeyReassignTests(TestCase):
    def setUp(self):
        dept = Department.objects.create(
            name=f"Dept-{uuid.uuid4().hex[:8]}",
            code=f"D{uuid.uuid4().hex[:4].upper()}",
            department_type=DepartmentType.INTERNAL,
        )
        self.equipment = Equipment.objects.create(
            name="EBSD test",
            code=f"EB{uuid.uuid4().hex[:4].upper()}",
            profile_type=EquipmentProfileType.GENERIC,
            slot_duration_minutes=180,
            internal_department=dept,
            status=EquipmentStatus.ACTIVE,
        )
        self.scans = DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.FACULTY,
            field_key="A",
            field_label="No. of scans",
            field_type=DynamicInputFieldType.NUMERIC,
        )
        self.admin_user = User.objects.create_superuser(
            email=f"admin-{uuid.uuid4().hex[:6]}@example.com", password="x"
        )

    def _formset(self, rows):
        request = RequestFactory().post("/")
        request.user = self.admin_user
        inline = DynamicInputFieldInline(Equipment, admin.site)
        FormSet = inline.get_formset(request, self.equipment)
        prefix = FormSet.get_default_prefix()
        initial = [r for r in rows if r.get("id")]
        data = {
            f"{prefix}-TOTAL_FORMS": str(len(rows)),
            f"{prefix}-INITIAL_FORMS": str(len(initial)),
            f"{prefix}-MIN_NUM_FORMS": "0",
            f"{prefix}-MAX_NUM_FORMS": "1000",
        }
        for i, row in enumerate(rows):
            base = {
                "id": "",
                "equipment": str(self.equipment.pk),
                "user_type": UserType.FACULTY,
                "field_type": DynamicInputFieldType.NUMERIC,
                "options_text": "",
                "help_text": "",
                "default_value": "",
                "source_element_field_key": "",
            }
            base.update(row)
            for key, value in base.items():
                if value is True:
                    data[f"{prefix}-{i}-{key}"] = "on"
                elif value is not False:
                    data[f"{prefix}-{i}-{key}"] = str(value)
        return FormSet(data=data, instance=self.equipment, prefix=prefix)

    def test_rename_a_to_b_and_add_new_a_in_one_save(self):
        formset = self._formset([
            {"id": self.scans.pk, "field_key": "B", "field_label": "No. of scans"},
            {"field_key": "A", "field_label": "No. of samples"},
        ])
        self.assertTrue(formset.is_valid(), formset.errors or formset.non_form_errors())
        formset.save()

        rows = dict(
            DynamicInputField.objects.filter(equipment=self.equipment).values_list("field_key", "field_label")
        )
        self.assertEqual(rows, {"A": "No. of samples", "B": "No. of scans"})
        self.scans.refresh_from_db()
        self.assertEqual((self.scans.field_key, self.scans.user_type), ("B", UserType.FACULTY))

    def test_swap_two_existing_keys(self):
        samples = DynamicInputField.objects.create(
            equipment=self.equipment,
            user_type=UserType.FACULTY,
            field_key="B",
            field_label="No. of samples",
            field_type=DynamicInputFieldType.NUMERIC,
        )
        formset = self._formset([
            {"id": self.scans.pk, "field_key": "B", "field_label": "No. of scans"},
            {"id": samples.pk, "field_key": "A", "field_label": "No. of samples"},
        ])
        self.assertTrue(formset.is_valid(), formset.errors or formset.non_form_errors())
        formset.save()
        rows = dict(
            DynamicInputField.objects.filter(equipment=self.equipment).values_list("field_key", "field_label")
        )
        self.assertEqual(rows, {"A": "No. of samples", "B": "No. of scans"})

    def test_duplicate_key_in_submission_still_rejected(self):
        formset = self._formset([
            {"id": self.scans.pk, "field_key": "A", "field_label": "No. of scans"},
            {"field_key": "A", "field_label": "No. of samples"},
        ])
        self.assertFalse(formset.is_valid())
        self.assertEqual(DynamicInputField.objects.filter(equipment=self.equipment).count(), 1)

    def test_same_key_for_other_user_type_allowed(self):
        formset = self._formset([
            {"id": self.scans.pk, "field_key": "A", "field_label": "No. of scans"},
            {"field_key": "A", "field_label": "No. of scans", "user_type": UserType.STUDENT},
        ])
        self.assertTrue(formset.is_valid(), formset.errors or formset.non_form_errors())
        formset.save()
        self.assertEqual(DynamicInputField.objects.filter(equipment=self.equipment, field_key="A").count(), 2)
