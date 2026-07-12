# Ensure completed_at exists on equipment_booking (repair if 0048 was applied elsewhere or failed)

from django.db import migrations


def add_completed_at_if_missing(apps, schema_editor):
    """Add completed_at column if it does not exist (e.g. DB was out of sync)."""
    from django.db import connection
    with connection.cursor() as cursor:
        cursor.execute("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'equipment_booking' AND column_name = 'completed_at';
        """)
        if cursor.fetchone() is None:
            cursor.execute("""
                ALTER TABLE equipment_booking
                ADD COLUMN completed_at TIMESTAMP WITH TIME ZONE NULL;
            """)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0048_repeat_sample_request_and_completed_at"),
    ]

    operations = [
        migrations.RunPython(add_completed_at_if_missing, noop_reverse),
    ]
