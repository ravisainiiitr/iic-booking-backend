# Generated migration: add virtual_booking_id to Booking

from django.db import migrations, models


def backfill_virtual_booking_ids(apps, schema_editor):
    """Set virtual_booking_id for existing bookings: equipment_code + year + 5-digit sequence."""
    Booking = apps.get_model('equipment', 'Booking')
    Equipment = apps.get_model('equipment', 'Equipment')
    from django.utils import timezone
    from itertools import groupby

    bookings = list(
        Booking.objects
        .filter(virtual_booking_id__isnull=True)
        .select_related('equipment')
        .order_by('equipment_id', 'created_at')
    )
    if not bookings:
        return

    # Build (equipment_id, year) -> list of bookings
    def key(b):
        year = b.created_at.year if b.created_at else timezone.now().year
        return (b.equipment_id, year)

    for (equipment_id, year), group in groupby(bookings, key=key):
        group_list = list(group)
        try:
            equipment = Equipment.objects.get(pk=equipment_id)
            code = (equipment.code or '').strip()
        except Equipment.DoesNotExist:
            code = ''
        for i, booking in enumerate(group_list):
            booking.virtual_booking_id = f"{code}{year}{i:05d}"
    Booking.objects.bulk_update(bookings, ['virtual_booking_id'])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0031_equipment_important_instruction'),
    ]

    operations = [
        migrations.AddField(
            model_name='booking',
            name='virtual_booking_id',
            field=models.CharField(
                blank=True,
                help_text='Display ID: equipment code + year + 5-digit sequence (e.g. GEM202600001)',
                max_length=32,
                null=True,
                unique=True,
                verbose_name='Virtual Booking ID',
            ),
        ),
        migrations.RunPython(backfill_virtual_booking_ids, noop_reverse),
        migrations.AddIndex(
            model_name='booking',
            index=models.Index(fields=['virtual_booking_id'], name='equipment_b_virtual_7a0b0d_idx'),
        ),
    ]
