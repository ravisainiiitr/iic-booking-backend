"""Validate LegacyEquipmentMapping rows (read-only)."""

from django.core.management.base import BaseCommand

from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings


class Command(BaseCommand):
    help = "Validate OLD→NEW equipment mappings. No writes."

    def handle(self, *args, **options):
        report = validate_legacy_equipment_mappings()
        self.stdout.write(f"mapped={report['counts']['mapped']}")
        self.stdout.write(f"unmapped={report['counts']['unmapped']}")
        self.stdout.write(f"conflict={report['counts']['conflict']}")
        self.stdout.write(f"disabled={report['counts']['disabled']}")
        self.stdout.write(f"invalid={report['counts']['invalid']}")
        if report["ready"]:
            self.stdout.write(self.style.SUCCESS("MAPPING VALIDATION OK"))
        else:
            self.stdout.write(self.style.ERROR("MAPPING VALIDATION HAS CONFLICTS/INVALID"))
            for row in report["conflict"][:50]:
                self.stdout.write(f"  CONFLICT {row}")
            for row in report["invalid"][:50]:
                self.stdout.write(f"  INVALID {row}")
