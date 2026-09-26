"""Auto-filled notice text for equipment set Under Maintenance."""

from datetime import date
from types import SimpleNamespace

from django.test import SimpleTestCase

from iic_booking.communication.notice_board_service import (
    build_equipment_unavailable_copy,
    equipment_display_label,
)


def _equipment(**overrides):
    base = {
        "pk": 7,
        "code": "EPR",
        "name": "Electron Paramagnetic Resonance (EPR)",
        "location": "Block-D, Ground Floor, IIC.",
        "internal_department": SimpleNamespace(name="Institute Instrumentation Centre"),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class EquipmentDisplayLabelTests(SimpleTestCase):
    def test_code_already_in_name_is_not_repeated(self):
        self.assertEqual(
            equipment_display_label(_equipment()),
            "Electron Paramagnetic Resonance (EPR)",
        )

    def test_code_appended_when_missing_from_name(self):
        self.assertEqual(
            equipment_display_label(_equipment(name="Field Emission SEM", code="FESEM")),
            "Field Emission SEM (FESEM)",
        )

    def test_falls_back_to_code_then_pk(self):
        self.assertEqual(equipment_display_label(_equipment(name="")), "EPR")
        self.assertEqual(equipment_display_label(_equipment(name="", code="")), "Equipment #7")


class EquipmentUnavailableCopyTests(SimpleTestCase):
    def test_title_and_description(self):
        title, description = build_equipment_unavailable_copy(
            _equipment(), since=date(2026, 9, 25)
        )
        self.assertEqual(title, "Electron Paramagnetic Resonance (EPR) — Under Maintenance")
        lines = description.split("\n")
        self.assertEqual(
            lines[0],
            "Electron Paramagnetic Resonance (EPR) is under maintenance from 25 Sep 2026 "
            "and is not available for booking until further notice.",
        )
        self.assertIn("Location: Block-D, Ground Floor, IIC.", lines)
        self.assertIn("Facility: Institute Instrumentation Centre.", lines)
        self.assertTrue(lines[-1].startswith("This notice will be removed automatically"))
        self.assertNotIn("(EPR) (EPR)", description)
        self.assertNotIn("/ unavailable", description)

    def test_optional_lines_omitted_when_blank(self):
        _, description = build_equipment_unavailable_copy(
            _equipment(location="  ", internal_department=None), since=date(2026, 1, 5)
        )
        self.assertIn("from 5 Jan 2026", description)
        self.assertNotIn("Location:", description)
        self.assertNotIn("Facility:", description)

    def test_long_title_fits_field(self):
        title, _ = build_equipment_unavailable_copy(
            _equipment(name="X" * 400, code="LONG"), since=date(2026, 1, 5)
        )
        self.assertLessEqual(len(title), 255)
        self.assertTrue(title.endswith("— Under Maintenance"))
