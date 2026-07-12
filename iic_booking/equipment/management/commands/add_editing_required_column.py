"""
One-off command to add editing_required column to equipment_dynamicinputfield.
Run if migrations are stuck and the column is missing:
  python manage.py add_editing_required_column
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Add editing_required column to equipment_dynamicinputfield if it does not exist (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return
        with connection.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE equipment_dynamicinputfield
                ADD COLUMN IF NOT EXISTS editing_required BOOLEAN NOT NULL DEFAULT FALSE;
            """)
        self.stdout.write(self.style.SUCCESS("Column equipment_dynamicinputfield.editing_required is present."))
