"""
Create (or complete) the test-only fabrication equipment:

  TEST-LASER-01  "Sample 2D Laser Cutter (TEST)"  LASER_CUT_2D, 10 sheet materials (8 ft x 4 ft)
  TEST-3DP-01    "Sample 3D Printer (TEST)"       PRINT_3D, 11 materials priced per gram

Both belong to the IIC department and are visible to test accounts only. Safe to run again: missing
pieces are added and nothing an administrator edited later (prices, charges, emails) is overwritten,
unless --reset-prices is given (resets material prices and the own-material charge to the seed values).

Own-material fixed charges handwritten on the IIC price sheet (Rs per booking). Only the two test machines are
seeded; real machines get theirs from their OIC / admin on the Fabrication Materials page:

  FDM printer           100   (TEST-3DP-01 uses this)
  Formlabs Form 4L      250
  MJP printer           250
  SLA printer           150   (handwriting unclear: confirm with the lab before setting it)
  SLS printer           250
  Metal laser (printer) 250
  CO2 laser cutter      250   (TEST-LASER-01 uses this)

Run:
  python manage.py seed_fabrication_test_equipment            # dry run
  python manage.py seed_fabrication_test_equipment --apply
"""

from datetime import time as dt_time
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from iic_booking.equipment.models import (
    DEFAULT_LASER_SHEET_HEIGHT_MM,
    DEFAULT_LASER_SHEET_WIDTH_MM,
    ChargeProfile,
    ChargeProfilePricingProfile,
    DynamicInputField,
    DynamicInputFieldType,
    Equipment,
    EquipmentCategory,
    EquipmentManager,
    EquipmentOperator,
    EquipmentProfileType,
    EquipmentStatus,
    LaserMaterialFamily,
    LaserSheetMaterial,
    PrintMaterial,
    PrintMaterialSourceUnit,
    SlotMaster,
    price_per_gram_from_source,
)
from iic_booking.users.models.department import Department
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType

LASER_CODE = "TEST-LASER-01"
PRINTER_CODE = "TEST-3DP-01"
DEPARTMENT_CODE = "IIC"
TEST_MANAGER_EMAIL = "test.manager@iic-booking.test"
TEST_OPERATOR_EMAIL = "test.operator@iic-booking.test"

# Same user types and zero rates as the existing DSATEST test equipment.
CHARGE_USER_TYPES = (UserType.FACULTY, UserType.STARTUP_INCUBATED_IITR, UserType.STUDENT)

SLOT_DEFS = [
    (1, dt_time(9, 0), dt_time(10, 0)),
    (2, dt_time(10, 0), dt_time(11, 0)),
    (3, dt_time(11, 0), dt_time(12, 0)),
    (4, dt_time(12, 0), dt_time(13, 0)),
    (5, dt_time(14, 0), dt_time(15, 0)),
    (6, dt_time(15, 0), dt_time(16, 0)),
    (7, dt_time(16, 0), dt_time(17, 0)),
    (8, dt_time(17, 0), dt_time(18, 0)),
]

# (code, name, family, thickness mm, rate per 8 ft x 4 ft sheet)
LASER_SHEETS = [
    ("MS-1", "MS sheet 1 mm", LaserMaterialFamily.MS, "1", "6026.40"),
    ("MS-3", "MS sheet 3 mm", LaserMaterialFamily.MS, "3", "8340.00"),
    ("SS-1", "SS sheet 1 mm", LaserMaterialFamily.SS, "1", "10653.60"),
    ("SS-2", "SS sheet 2 mm", LaserMaterialFamily.SS, "2", "23202.00"),
    ("SS-3", "SS sheet 3 mm", LaserMaterialFamily.SS, "3", "32042.40"),
    ("ACR-2", "Acrylic sheet 2 mm", LaserMaterialFamily.ACRYLIC, "2", "4460.40"),
    ("ACR-3", "Acrylic sheet 3 mm", LaserMaterialFamily.ACRYLIC, "3", "6018.00"),
    ("ACR-5", "Acrylic sheet 5 mm", LaserMaterialFamily.ACRYLIC, "5", "9741.60"),
    ("MDF-2", "MDF sheet 2 mm", LaserMaterialFamily.MDF, "2", "2102.40"),
    ("MDF-3", "MDF sheet 3 mm", LaserMaterialFamily.MDF, "3", "2505.60"),
]

KG = PrintMaterialSourceUnit.PER_KG
LITRE = PrintMaterialSourceUnit.PER_LITRE
# (code, name incl. process, density g/cm3, supplier rate, unit)
PRINT_MATERIALS = [
    ("PLA-FDM", "PLA (FDM)", "1.24", "1440.00", KG),
    ("ABS-FDM", "ABS (FDM)", "1.04", "1486.80", KG),
    ("PETG-FDM", "PETG (FDM)", "1.27", "2280.00", KG),
    ("TPU-FDM", "TPU (FDM)", "1.21", "3625.20", KG),
    ("RESIN-GREY-V5", "Resin Grey v5 (Formlabs Form 4L)", "1.18", "16800.00", LITRE),
    ("VISIJET-M3X", "Visijet M3X (MJP 3600 Max)", "1.02", "43200.00", LITRE),
    ("VISIJET-M3-NAVY", "Visijet M3 Navy (MJP 3600 Max)", "1.02", "36000.00", LITRE),
    ("VISIJET-S300", "Visijet support S300 (MJP 3600 Max)", "0.88", "33600.00", LITRE),
    ("ABS-LIKE-RESIN", "ABS-like resin (SLA)", "1.10", "2400.00", LITRE),
    ("PA12-FRESH", "PA 12 smooth fresh (SLS)", "1.01", "26869.20", KG),
    ("PA12-STARTER", "PA 12 smooth starter (SLS)", "1.01", "25770.00", KG),
]

EQUIPMENT_SPECS = {
    LASER_CODE: {
        "name": "Sample 2D Laser Cutter (TEST)",
        "profile_type": EquipmentProfileType.LASER_CUT_2D,
        "own_material_fixed_charge": Decimal("250.00"),
        "description": (
            "Test-only 2D laser cutter. Upload DXF drawings; material is charged by the share of an "
            "8 ft x 4 ft sheet each part uses. Bring your own material for a fixed charge."
        ),
    },
    PRINTER_CODE: {
        "name": "Sample 3D Printer (TEST)",
        "profile_type": EquipmentProfileType.PRINT_3D,
        "own_material_fixed_charge": Decimal("100.00"),
        "description": (
            "Test-only 3D printer. Upload STL files; material is charged per gram and time per hour. "
            "Bring your own material for a fixed charge."
        ),
    },
}


class Command(BaseCommand):
    help = "Create the test-only 2D laser cutter and 3D printer (idempotent; dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write changes (default is a dry run).")
        parser.add_argument(
            "--reset-prices",
            action="store_true",
            help="Also reset material prices and the own-material charge to the seed values.",
        )

    def handle(self, *args, **options):
        apply = bool(options.get("apply"))
        self.reset_prices = bool(options.get("reset_prices"))
        self.actions = []
        department = Department.objects.filter(code=DEPARTMENT_CODE).first()
        if department is None:
            raise CommandError(f"Department with code '{DEPARTMENT_CODE}' not found.")

        with transaction.atomic():
            for code, spec in EQUIPMENT_SPECS.items():
                equipment = self._equipment(code, spec, department)
                self._charge_profiles(equipment, spec["profile_type"])
                self._slots(equipment)
                self._project_field(equipment)
                self._staff(equipment)
                if spec["profile_type"] == EquipmentProfileType.LASER_CUT_2D:
                    self._laser_sheets(equipment)
                else:
                    self._print_materials(equipment)
            for line in self.actions:
                self.stdout.write(line)
            if not self.actions:
                self.stdout.write("Nothing to do: the test fabrication equipment is complete.")
            if not apply:
                transaction.set_rollback(True)
                self.stdout.write(self.style.WARNING("Dry run: nothing was saved. Re-run with --apply."))
                return
        self.stdout.write(self.style.SUCCESS(f"Applied {len(self.actions)} change(s)."))

    def _log(self, text):
        self.actions.append(f"  {text}")

    def _equipment(self, code, spec, department):
        equipment = Equipment.objects.filter(code=code).first()
        if equipment is None:
            category, _ = EquipmentCategory.objects.get_or_create(
                code="SAMPLE-CAT",
                defaults={"name": "Sample Category", "description": "For sample equipment"},
            )
            equipment = Equipment.objects.create(
                code=code,
                name=spec["name"],
                description=spec["description"],
                status=EquipmentStatus.ACTIVE,
                profile_type=spec["profile_type"],
                category=category,
                internal_department=department,
                visible_to_test_accounts_only=True,
                slot_duration_minutes=60,
                slots_per_day=len(SLOT_DEFS),
                enable_charge_recalculation=True,
                own_material_fixed_charge=spec["own_material_fixed_charge"],
                fabrication_notification_emails=[],
            )
            self._log(f"created equipment {code} ({spec['name']})")
            return equipment

        if equipment.profile_type != spec["profile_type"]:
            raise CommandError(
                f"Equipment {code} exists with profile {equipment.profile_type}, expected {spec['profile_type']}."
            )
        update_fields = []
        if not equipment.visible_to_test_accounts_only:
            equipment.visible_to_test_accounts_only = True
            update_fields.append("visible_to_test_accounts_only")
        if equipment.internal_department_id != department.pk:
            equipment.internal_department = department
            update_fields.append("internal_department")
        if self.reset_prices and equipment.own_material_fixed_charge != spec["own_material_fixed_charge"]:
            equipment.own_material_fixed_charge = spec["own_material_fixed_charge"]
            update_fields.append("own_material_fixed_charge")
        if update_fields:
            equipment.save(update_fields=update_fields + ["updated_at"])
            self._log(f"{code}: updated {', '.join(update_fields)}")
        return equipment

    def _charge_profiles(self, equipment, profile_type):
        for user_type in CHARGE_USER_TYPES:
            for pricing in (ChargeProfilePricingProfile.STANDARD, ChargeProfilePricingProfile.DISCOUNTED):
                _cp, created = ChargeProfile.objects.get_or_create(
                    equipment=equipment,
                    user_type=user_type,
                    pricing_profile=pricing,
                    defaults={
                        "profile_type": profile_type,
                        "is_active": True,
                        "primary_unit_charge": Decimal("0.00"),
                        "secondary_unit_charge": Decimal("0.00"),
                        "time_formula": "",
                    },
                )
                if created:
                    self._log(f"{equipment.code}: charge profile {user_type} / {pricing} (rate 0)")

    def _slots(self, equipment):
        for number, open_time, close_time in SLOT_DEFS:
            _slot, created = SlotMaster.objects.get_or_create(
                equipment=equipment,
                slot_number=number,
                defaults={"slot_name": f"Slot {number}", "open_time": open_time, "close_time": close_time, "is_active": True},
            )
            if created:
                self._log(f"{equipment.code}: slot {number} {open_time:%H:%M}-{close_time:%H:%M}")

    def _project_field(self, equipment):
        for user_type in CHARGE_USER_TYPES:
            _field, created = DynamicInputField.objects.get_or_create(
                equipment=equipment,
                user_type=user_type,
                field_key="D",
                defaults={
                    "field_label": "Project Title",
                    "field_type": DynamicInputFieldType.TEXT,
                    "is_required": False,
                },
            )
            if created:
                self._log(f"{equipment.code}: optional field D 'Project Title' for {user_type}")

    def _staff(self, equipment):
        manager = User.objects.filter(email=TEST_MANAGER_EMAIL, user_type=UserType.MANAGER).first()
        if manager and not EquipmentManager.objects.filter(equipment=equipment, manager=manager).exists():
            EquipmentManager.objects.create(equipment=equipment, manager=manager)
            self._log(f"{equipment.code}: OIC {manager.email}")
        operator = User.objects.filter(email=TEST_OPERATOR_EMAIL, user_type=UserType.OPERATOR).first()
        if operator and not EquipmentOperator.objects.filter(equipment=equipment, operator=operator).exists():
            if not EquipmentOperator.objects.filter(equipment=equipment, role=EquipmentOperator.Role.PRIMARY).exists():
                EquipmentOperator.objects.create(equipment=equipment, operator=operator, role=EquipmentOperator.Role.PRIMARY)
                self._log(f"{equipment.code}: operator {operator.email}")

    def _laser_sheets(self, equipment):
        for order, (code, name, family, thickness, rate) in enumerate(LASER_SHEETS):
            values = {
                "name": name,
                "material_family": family,
                "thickness_mm": Decimal(thickness),
                "sheet_width_mm": DEFAULT_LASER_SHEET_WIDTH_MM,
                "sheet_height_mm": DEFAULT_LASER_SHEET_HEIGHT_MM,
                "sheet_rate": Decimal(rate),
                "display_order": order,
                "is_active": True,
            }
            material = LaserSheetMaterial.objects.filter(equipment=equipment, code=code).first()
            if material is None:
                LaserSheetMaterial.objects.create(equipment=equipment, code=code, **values)
                self._log(f"{equipment.code}: sheet {code} {name} @ {rate}/sheet")
            elif self.reset_prices and material.sheet_rate != Decimal(rate):
                material.sheet_rate = Decimal(rate)
                material.save(update_fields=["sheet_rate", "updated_at"])
                self._log(f"{equipment.code}: sheet {code} rate reset to {rate}")

    def _print_materials(self, equipment):
        for order, (code, name, density, rate, unit) in enumerate(PRINT_MATERIALS):
            density_d = Decimal(density)
            rate_d = Decimal(rate)
            price = price_per_gram_from_source(rate_d, unit, density_d)
            material = PrintMaterial.objects.filter(equipment=equipment, code=code).first()
            if material is None:
                PrintMaterial.objects.create(
                    equipment=equipment,
                    code=code,
                    name=name,
                    density_g_per_cm3=density_d,
                    price_per_gram=price,
                    source_rate=rate_d,
                    source_unit=unit,
                    display_order=order,
                    is_active=True,
                )
                self._log(f"{equipment.code}: material {code} {name} {rate}/{unit} -> {price}/g")
            elif self.reset_prices and (material.price_per_gram != price or material.source_rate != rate_d):
                material.price_per_gram = price
                material.source_rate = rate_d
                material.source_unit = unit
                material.density_g_per_cm3 = density_d
                material.save(update_fields=["price_per_gram", "source_rate", "source_unit", "density_g_per_cm3", "updated_at"])
                self._log(f"{equipment.code}: material {code} price reset to {price}/g")
