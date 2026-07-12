"""
One-off command to add important_instruction column to equipment_equipment.
Run if migration 0031 cannot be applied (e.g. migrations out of sync):
  python manage.py add_important_instruction_column
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Add important_instruction column to equipment_equipment if it does not exist (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return
        with connection.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE equipment_equipment
                ADD COLUMN IF NOT EXISTS important_instruction TEXT NULL;
            """)
        self.stdout.write(self.style.SUCCESS("Column equipment_equipment.important_instruction is present."))
