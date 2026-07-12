"""Seed sample PRINT_3D equipment (SAMPLE-3DP-01) for testing."""

from datetime import time as dt_time
from decimal import Decimal

from django.db import migrations


def seed_sample_print_3d_equipment(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    EquipmentCategory = apps.get_model("equipment", "EquipmentCategory")
    PrintMaterial = apps.get_model("equipment", "PrintMaterial")
    ChargeProfile = apps.get_model("equipment", "ChargeProfile")
    DynamicInputField = apps.get_model("equipment", "DynamicInputField")
    SlotMaster = apps.get_model("equipment", "SlotMaster")

    code = "SAMPLE-3DP-01"
    if Equipment.objects.filter(code=code).exists():
        return

    category, _ = EquipmentCategory.objects.get_or_create(
        code="SAMPLE-CAT",
        defaults={"name": "Sample Category", "description": "For sample equipment"},
    )

    equipment = Equipment.objects.create(
        name="Sample 3D Printer (FDM)",
        code=code,
        description=(
            "Sample FDM 3D printer for testing PRINT_3D bookings. "
            "Upload STL to get weight/time quote."
        ),
        status="ACTIVE",
        profile_type="PRINT_3D",
        category=category,
        slot_duration_minutes=60,
        slots_per_day=8,
        location="Additive Manufacturing Lab, Block C",
        enable_charge_recalculation=True,
        make="Sample OEM",
        show_make_on_card=True,
    )

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

    charge_rates = {
        "student": Decimal("300.00"),
        "faculty": Decimal("400.00"),
        "RND": Decimal("500.00"),
        "external": Decimal("600.00"),
    }
    for user_type, hourly_rate in charge_rates.items():
        ChargeProfile.objects.create(
            equipment=equipment,
            user_type=user_type,
            pricing_profile="standard",
            is_active=True,
            primary_unit_charge=hourly_rate,
            secondary_unit_charge=Decimal("0.00"),
            time_formula="",
        )
        ChargeProfile.objects.create(
            equipment=equipment,
            user_type=user_type,
            pricing_profile="discounted",
            is_active=True,
            primary_unit_charge=Decimal("0.00"),
            secondary_unit_charge=Decimal("0.00"),
            time_formula="",
        )

    DynamicInputField.objects.create(
        equipment=equipment,
        field_key="D",
        field_label="Project / part name",
        field_type="TEXT",
        is_required=False,
        help_text="Optional label for your print job.",
    )

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


def unseed_sample_print_3d_equipment(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    Equipment.objects.filter(code="SAMPLE-3DP-01").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0147_print_3d"),
    ]

    operations = [
        migrations.RunPython(seed_sample_print_3d_equipment, unseed_sample_print_3d_equipment),
    ]
