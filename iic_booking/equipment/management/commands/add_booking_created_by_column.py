"""
One-off command to add created_by_id column to equipment_booking.
Run if migration 0040 cannot be applied (e.g. migrations out of sync):
  python manage.py add_booking_created_by_column
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Add created_by_id column to equipment_booking (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return

        user_table = get_user_model()._meta.db_table

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                ALTER TABLE equipment_booking
                ADD COLUMN IF NOT EXISTS created_by_id INTEGER NULL
                    REFERENCES {user_table}(id) ON DELETE SET NULL;
                """
            )
        self.stdout.write(
            self.style.SUCCESS("Column equipment_booking.created_by_id is present.")
        )
