"""
One-off command to add results_available_notified_at column to equipment_booking.
Run if migration 0033 cannot be applied (e.g. migrations out of sync):
  python manage.py add_results_available_notified_at_column
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Add results_available_notified_at column to equipment_booking (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return
        with connection.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE equipment_booking
                ADD COLUMN IF NOT EXISTS results_available_notified_at TIMESTAMP WITH TIME ZONE NULL;
            """)
        self.stdout.write(self.style.SUCCESS("Column equipment_booking.results_available_notified_at is present."))
