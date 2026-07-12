"""
Create a sample equipment with Multi-parameter profile type (and slot options).

Use this when the admin add form fails for MULTI_PARAM so you have a working
example. Run:
  python manage.py create_sample_multiparam_equipment

Optional: --code SAMPLE-XRD to use a different code (default: SAMPLE-MULTIPARAM-01).
          --skip-existing  Do not create if equipment with this code already exists.
"""
from decimal import Decimal
from datetime import time as dt_time

from django.core.management.base import BaseCommand

from iic_booking.users.models.user_type import UserType
from iic_booking.equipment.models import (
    Equipment,
    EquipmentCategory,
    EquipmentProfileType,
    EquipmentStatus,
    ChargeProfilePricingProfile,
    MultiParamDefinition,
    SlotMaster,
    ChargeProfile,
)


class Command(BaseCommand):
    help = "Create a sample equipment with profile_type=MULTI_PARAM and slot options."

    def add_arguments(self, parser):
        parser.add_argument(
            "--code",
            type=str,
            default="SAMPLE-MULTIPARAM-01",
            help="Equipment code (unique). Default: SAMPLE-MULTIPARAM-01",
        )
        parser.add_argument(
            "--skip-existing",
            action="store_true",
            help="Skip if equipment with this code already exists.",
        )

    def handle(self, *args, **options):
        code = (options.get("code") or "SAMPLE-MULTIPARAM-01").strip()
        skip_existing = options.get("skip_existing", False)

        if Equipment.objects.filter(code=code).exists():
            if skip_existing:
                self.stdout.write(self.style.WARNING(f"Equipment with code '{code}' already exists. Skipping."))
                return
            self.stdout.write(self.style.ERROR(f"Equipment with code '{code}' already exists. Use another --code or --skip-existing."))
            return

        # Get or create a category
        category, _ = EquipmentCategory.objects.get_or_create(
            code="SAMPLE-CAT",
            defaults={"name": "Sample Category", "description": "For sample equipment"},
        )

        # 1. Create equipment with MULTI_PARAM profile
        equipment = Equipment(
            name="Sample Multi-Parameter Equipment (XRD)",
            code=code,
            description="Sample equipment created by create_sample_multiparam_equipment for testing multi-parameter profile.",
            status=EquipmentStatus.ACTIVE,
            profile_type=EquipmentProfileType.MULTI_PARAM,
            category=category,
            slot_duration_minutes=30,
            slots_per_day=12,
            location="Block A, Lab 101",
        )
        equipment.save()
        self.stdout.write(self.style.SUCCESS(f"Created equipment: {equipment.code} ({equipment.name})"))

        # 2. Slot options (MultiParamDefinition) – Student: 1 Slot, 2 Slots; Faculty: 1 Slot, 2 Slots
        slot_options = [
            # (user_type, param_name, param_code, unit_time_minutes, unit_charge)
            (UserType.STUDENT, "1 Slot", "1", 60, Decimal("100.00")),
            (UserType.STUDENT, "2 Slots", "2", 120, Decimal("180.00")),
            (UserType.FACULTY, "1 Slot", "1", 60, Decimal("150.00")),
            (UserType.FACULTY, "2 Slots", "2", 120, Decimal("270.00")),
        ]
        for user_type, param_name, param_code, unit_time_min, unit_charge in slot_options:
            MultiParamDefinition.objects.create(
                equipment=equipment,
                user_type=user_type,
                param_name=param_name,
                param_code=param_code,
                unit_time_minutes=unit_time_min,
                unit_charge=unit_charge,
                is_active=True,
            )
        self.stdout.write(self.style.SUCCESS(f"  Created {len(slot_options)} slot option(s) (param_definitions)."))

        # 3. Charge profiles (required for booking; MULTI_PARAM uses param_def for actual charge)
        for user_type in (UserType.STUDENT, UserType.FACULTY):
            ChargeProfile.objects.create(
                equipment=equipment,
                user_type=user_type,
                pricing_profile=ChargeProfilePricingProfile.STANDARD,
                is_active=True,
                primary_unit_charge=Decimal("100.00"),
                secondary_unit_charge=Decimal("0.00"),
            )
            ChargeProfile.objects.create(
                equipment=equipment,
                user_type=user_type,
                pricing_profile=ChargeProfilePricingProfile.DISCOUNTED,
                is_active=True,
                primary_unit_charge=Decimal("0.00"),
                secondary_unit_charge=Decimal("0.00"),
            )
        self.stdout.write(self.style.SUCCESS("  Created charge profiles for Student and Faculty."))

        # 4. Slot masters (two 30-min slots)
        SlotMaster.objects.create(
            equipment=equipment,
            slot_number=1,
            slot_name="Morning-1",
            open_time=dt_time(9, 0),
            close_time=dt_time(9, 30),
            is_active=True,
        )
        SlotMaster.objects.create(
            equipment=equipment,
            slot_number=2,
            slot_name="Morning-2",
            open_time=dt_time(9, 30),
            close_time=dt_time(10, 0),
            is_active=True,
        )
        self.stdout.write(self.style.SUCCESS("  Created 2 slot masters (09:00–09:30, 09:30–10:00)."))

        self.stdout.write(self.style.SUCCESS(f"Done. You can edit this equipment in admin: Equipment > Equipments > {equipment.code}"))
