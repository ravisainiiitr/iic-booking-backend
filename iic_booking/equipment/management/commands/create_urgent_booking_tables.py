"""
One-off command to create equipment_noslotallocationlog and equipment_urgentbookingrequest tables.
Run if migration 0041 cannot be applied:
  python manage.py create_urgent_booking_tables
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Create no-slot-allocation log and urgent-booking-request tables (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return

        user_table = get_user_model()._meta.db_table

        with connection.cursor() as cursor:
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS equipment_noslotallocationlog (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES {user_table}(id) ON DELETE CASCADE,
                    equipment_id INTEGER NOT NULL REFERENCES equipment_equipment(equipment_id) ON DELETE CASCADE,
                    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    number_of_samples INTEGER NOT NULL DEFAULT 1,
                    slots_requested INTEGER NOT NULL DEFAULT 1,
                    duration_minutes INTEGER NULL
                );
                """
            )
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS equipment_urgentbookingrequest (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES {user_table}(id) ON DELETE CASCADE,
                    equipment_id INTEGER NOT NULL REFERENCES equipment_equipment(equipment_id) ON DELETE CASCADE,
                    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    disclaimer_accepted BOOLEAN NOT NULL DEFAULT FALSE,
                    number_of_samples INTEGER NOT NULL DEFAULT 1,
                    slots_requested INTEGER NOT NULL DEFAULT 1,
                    duration_minutes INTEGER NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
                    admin_notes TEXT NOT NULL DEFAULT '',
                    decided_at TIMESTAMPTZ NULL,
                    decided_by_id INTEGER NULL REFERENCES {user_table}(id) ON DELETE SET NULL
                );
                """
            )
        self.stdout.write(
            self.style.SUCCESS("Tables equipment_noslotallocationlog and equipment_urgentbookingrequest are present.")
        )
