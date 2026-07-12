"""
Create a sample equipment with 3D Print (PRINT_3D) profile type.

Includes print materials, charge profiles, slot masters, and an optional project-name field.

Run:
  python manage.py create_sample_print_3d_equipment

Optional:
  --code SAMPLE-3DP-01
  --skip-existing
"""
from datetime import time as dt_time
from decimal import Decimal

from django.core.management.base import BaseCommand

from iic_booking.equipment.models import (
    ChargeProfile,
    ChargeProfilePricingProfile,
    DynamicInputField,
    DynamicInputFieldType,
    Equipment,
    EquipmentCategory,
    EquipmentProfileType,
    EquipmentStatus,
    PrintMaterial,
    SlotMaster,
)
from iic_booking.users.models.user_type import UserType


class Command(BaseCommand):
    help = "Create a sample equipment with profile_type=PRINT_3D, materials, and charge profiles."

    def add_arguments(self, parser):
        parser.add_argument(
            "--code",
            type=str,
            default="SAMPLE-3DP-01",
            help="Equipment code (unique). Default: SAMPLE-3DP-01",
        )
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            help="Skip if equipment with this code already exists.",
        )

    def handle(self, *args, **options):
        code = (options.get("code") or "SAMPLE-3DP-01").strip()
        skip_existing = options.get("skip_existing", False)

        if Equipment.objects.filter(code=code).exists():
            if skip_existing:
                self.stdout.write(self.style.WARNING(f"Equipment with code '{code}' already exists. Skipping."))
                return
            self.stdout.write(
                self.style.ERROR(
                    f"Equipment with code '{code}' already exists. Use another --code or --skip-existing."
                )
            )
            return

        category, _ = EquipmentCategory.objects.get_or_create(
            code="SAMPLE-CAT",
            defaults={"name": "Sample Category", "description": "For sample equipment"},
        )

        equipment = Equipment(
            name="Sample 3D Printer (FDM)",
            code=code,
            description=(
                "Sample FDM 3D printer for testing PRINT_3D bookings. "
                "Upload STL to get weight/time quote; material cost is per gram; "
                "machine time is billed at the hourly rate on the charge profile."
            ),
            status=EquipmentStatus.ACTIVE,
            profile_type=EquipmentProfileType.PRINT_3D,
            category=category,
            slot_duration_minutes=60,
            slots_per_day=8,
            location="Additive Manufacturing Lab, Block C",
            enable_charge_recalculation=True,
            make="Sample OEM",
            show_make_on_card=True,
        )
        equipment.save()
        self.stdout.write(self.style.SUCCESS(f"Created equipment: {equipment.code} ({equipment.name})"))

        materials = [
            ("pla_white", "PLA White", Decimal("1.240"), Decimal("2.50"), 0),
            ("petg_black", "PETG Black", Decimal("1.270"), Decimal("3.00"), 1),
            ("abs_natural", "ABS Natural", Decimal("1.040"), Decimal("3.50"), 2),
        ]
        for mat_code, mat_name, density, price, order in materials:
            PrintMaterial.objects.create(
                equipment=equipment,
                code=mat_code,
                name=mat_name,
                density_g_per_cm3=density,
                price_per_gram=price,
                display_order=order,
                is_active=True,
            )
        self.stdout.write(self.style.SUCCESS(f"  Created {len(materials)} print material(s)."))

        # primary_unit_charge = machine hourly rate (₹/hour); material cost uses PrintMaterial.price_per_gram
        charge_rates = {
            UserType.STUDENT: Decimal("300.00"),
            UserType.FACULTY: Decimal("400.00"),
            UserType.RND: Decimal("500.00"),
            UserType.EXTERNAL: Decimal("600.00"),
            UserType.STARTUP_INCUBATED_IITR: Decimal("400.00"),
            UserType.EXTERNAL_STARTUP_MSME: Decimal("600.00"),
        }
        for user_type, hourly_rate in charge_rates.items():
            ChargeProfile.objects.create(
                equipment=equipment,
                user_type=user_type,
                pricing_profile=ChargeProfilePricingProfile.STANDARD,
                is_active=True,
                primary_unit_charge=hourly_rate,
                secondary_unit_charge=Decimal("0.00"),
                time_formula="",
            )
            ChargeProfile.objects.create(
                equipment=equipment,
                user_type=user_type,
                pricing_profile=ChargeProfilePricingProfile.DISCOUNTED,
                is_active=True,
                primary_unit_charge=Decimal("0.00"),
                secondary_unit_charge=Decimal("0.00"),
                time_formula="",
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"  Created charge profiles for {len(charge_rates)} user types "
                f"(+ discounted variants)."
            )
        )

        DynamicInputField.objects.create(
            equipment=equipment,
            field_key="D",
            field_label="Project / part name",
            field_type=DynamicInputFieldType.TEXT,
            is_required=False,
            help_text="Optional label for your print job (e.g. bracket prototype v2).",
        )
        self.stdout.write(self.style.SUCCESS("  Created optional input field D (project name)."))

        slot_defs = [
            (1, "Morning-1", dt_time(9, 0), dt_time(10, 0)),
            (2, "Morning-2", dt_time(10, 0), dt_time(11, 0)),
            (3, "Afternoon-1", dt_time(14, 0), dt_time(15, 0)),
            (4, "Afternoon-2", dt_time(15, 0), dt_time(16, 0)),
        ]
        for slot_number, slot_name, open_time, close_time in slot_defs:
            SlotMaster.objects.create(
                equipment=equipment,
                slot_number=slot_number,
                slot_name=slot_name,
                open_time=open_time,
                close_time=close_time,
                is_active=True,
            )
        self.stdout.write(self.style.SUCCESS(f"  Created {len(slot_defs)} slot masters (60 min each)."))

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Sample 3D printer is ready."))
        self.stdout.write(f"  Book: /book-equipment?equipment_id={equipment.equipment_id}")
        self.stdout.write(f"  STL test page: /test/print3d-analyzer")
        self.stdout.write(f"  Admin edit: Equipment > {equipment.code}")
