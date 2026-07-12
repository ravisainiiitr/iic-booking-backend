"""
One-off command to add user_reply column to equipment_bookingsampletrace.
Run if migration 0038 cannot be applied (e.g. migrations out of sync):
  python manage.py add_sample_trace_user_reply_column
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Add user_reply column to equipment_bookingsampletrace (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return
        with connection.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE equipment_bookingsampletrace
                ADD COLUMN IF NOT EXISTS user_reply TEXT NOT NULL DEFAULT '';
            """)
        self.stdout.write(self.style.SUCCESS("Column equipment_bookingsampletrace.user_reply is present."))
