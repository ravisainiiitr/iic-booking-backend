"""
One-off command to add attachment column to support_ticket.
Run if migration 0004 cannot be applied:
  python manage.py add_ticket_attachment_column
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Add attachment column to support_ticket (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return

        with connection.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE support_ticket
                ADD COLUMN IF NOT EXISTS attachment VARCHAR(100) NULL;
            """)
        self.stdout.write(
            self.style.SUCCESS("Column support_ticket.attachment is present.")
        )
