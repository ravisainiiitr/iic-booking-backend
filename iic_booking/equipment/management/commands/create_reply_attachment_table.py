"""
One-off command to create equipment_bookingsampletracereplyattachment table.
Run if migration 0039 cannot be applied (e.g. migrations out of sync):
  python manage.py create_reply_attachment_table
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Create equipment_bookingsampletracereplyattachment table (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return

        user_table = get_user_model()._meta.db_table

        with connection.cursor() as cursor:
            # user_table is from settings (e.g. users_user), safe to use as identifier
            cursor.execute(
                f"""
                CREATE TABLE IF NOT EXISTS equipment_bookingsampletracereplyattachment (
                    id SERIAL PRIMARY KEY,
                    sample_trace_id INTEGER NOT NULL
                        REFERENCES equipment_bookingsampletrace(id) ON DELETE CASCADE,
                    file VARCHAR(100) NOT NULL,
                    original_name VARCHAR(255) NOT NULL DEFAULT '',
                    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    uploaded_by_id INTEGER NULL
                        REFERENCES {user_table}(id) ON DELETE SET NULL
                );
                """
            )
        self.stdout.write(
            self.style.SUCCESS("Table equipment_bookingsampletracereplyattachment is present.")
        )
