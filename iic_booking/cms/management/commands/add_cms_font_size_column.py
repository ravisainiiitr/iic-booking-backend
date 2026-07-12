"""
One-off command to add font_size column to cms_homepagecontent.
Run if migration 0006 cannot be applied (e.g. migrations out of sync):
  python manage.py add_cms_font_size_column
"""
from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = "Add font_size column to cms_homepagecontent table (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return
        with connection.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE cms_homepagecontent
                ADD COLUMN IF NOT EXISTS font_size VARCHAR(30) NULL;
            """)
        self.stdout.write(self.style.SUCCESS("Column cms_homepagecontent.font_size is present."))
