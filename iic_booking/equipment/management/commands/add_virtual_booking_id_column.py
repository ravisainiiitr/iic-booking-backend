"""
One-off command to add virtual_booking_id column to equipment_booking and backfill existing rows.
Run if migration 0032 cannot be applied (e.g. migrations out of sync):
  python manage.py add_virtual_booking_id_column
"""
from django.core.management.base import BaseCommand
from django.db import connection
from itertools import groupby
from django.utils import timezone


class Command(BaseCommand):
    help = "Add virtual_booking_id column to equipment_booking and backfill (PostgreSQL)."

    def handle(self, *args, **options):
        if connection.vendor != "postgresql":
            self.stdout.write(self.style.WARNING("This command only runs on PostgreSQL. Skipping."))
            return
        with connection.cursor() as cursor:
            cursor.execute("""
                ALTER TABLE equipment_booking
                ADD COLUMN IF NOT EXISTS virtual_booking_id VARCHAR(32) NULL;
            """)
            try:
                cursor.execute("""
                    DO $$
                    BEGIN
                        IF NOT EXISTS (
                            SELECT 1 FROM pg_constraint
                            WHERE conname = 'equipment_booking_virtual_booking_id_key'
                            AND conrelid = 'equipment_booking'::regclass
                        ) THEN
                            ALTER TABLE equipment_booking
                            ADD CONSTRAINT equipment_booking_virtual_booking_id_key
                            UNIQUE (virtual_booking_id);
                        END IF;
                    END $$;
                """)
            except Exception:
                pass  # Constraint may already exist or fail on duplicate NULLs
        self.stdout.write(self.style.SUCCESS("Column equipment_booking.virtual_booking_id is present."))

        # Backfill existing rows: equipment_code + year + 5-digit sequence
        from iic_booking.equipment.models import Booking, Equipment
        bookings = list(
            Booking.objects.filter(virtual_booking_id__isnull=True)
            .select_related("equipment")
            .order_by("equipment_id", "created_at")
        )
        if not bookings:
            return
        def key(b):
            year = b.created_at.year if b.created_at else timezone.now().year
            return (b.equipment_id, year)
        for (equipment_id, year), group in groupby(bookings, key=key):
            group_list = list(group)
            try:
                equipment = Equipment.objects.get(pk=equipment_id)
                code = (equipment.code or "").strip()
            except Equipment.DoesNotExist:
                code = ""
            for i, booking in enumerate(group_list):
                booking.virtual_booking_id = f"{code}{year}{i:05d}"
        Booking.objects.bulk_update(bookings, ["virtual_booking_id"])
        self.stdout.write(self.style.SUCCESS(f"Backfilled virtual_booking_id for {len(bookings)} booking(s)."))
