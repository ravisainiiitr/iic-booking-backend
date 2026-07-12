"""
One-off command to create equipment_bookingsampletrace table.
Run if migration 0034 cannot be applied (e.g. migrations out of sync):
  python manage.py add_sample_trace_table
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Create equipment_bookingsampletrace table (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return
        with connection.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS equipment_bookingsampletrace (
                    id SERIAL PRIMARY KEY,
                    status VARCHAR(20) NOT NULL,
                    sample_identifiers TEXT NOT NULL DEFAULT '',
                    tracking_id TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
                    booking_id INTEGER NOT NULL REFERENCES equipment_booking(booking_id) ON DELETE CASCADE,
                    created_by_id INTEGER NULL REFERENCES users_user(id) ON DELETE SET NULL
                );
            """)
            cursor.execute("""
                ALTER TABLE equipment_bookingsampletrace
                ADD COLUMN IF NOT EXISTS tracking_id TEXT NOT NULL DEFAULT '';
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS equipment_b_booking_7a1b2c_idx
                ON equipment_bookingsampletrace (booking_id, created_at);
            """)
        self.stdout.write(self.style.SUCCESS("Table equipment_bookingsampletrace is present."))
