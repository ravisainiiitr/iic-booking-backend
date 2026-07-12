"""
One-off command to create equipment_bookingattemptlog table.
Run if migration 0042 cannot be applied (e.g. migration state out of sync):
  python manage.py create_booking_attempt_log_table
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Create booking attempt log table (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return

        user_table = get_user_model()._meta.db_table

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS equipment_bookingattemptlog (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES {user_table}(id) ON DELETE CASCADE,
                    equipment_id INTEGER NOT NULL REFERENCES equipment_equipment(equipment_id) ON DELETE CASCADE,
                    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    outcome VARCHAR(20) NOT NULL,
                    failure_reason TEXT NOT NULL DEFAULT '',
                    number_of_samples INTEGER NOT NULL DEFAULT 1,
                    slots_requested INTEGER NOT NULL DEFAULT 1,
                    duration_minutes INTEGER NULL,
                    booking_id INTEGER NULL
                );
                """
            )
        self.stdout.write(
            self.style.SUCCESS("Table equipment_bookingattemptlog is present.")
        )
