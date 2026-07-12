# Create equipment_bookingresultfile table if missing (e.g. migration 0027 not applied or table dropped)

from django.db import migrations


def create_table_if_not_exists(apps, schema_editor):
    """Create equipment_bookingresultfile table for PostgreSQL if it does not exist."""
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS equipment_bookingresultfile (
                id BIGSERIAL PRIMARY KEY,
                booking_id INTEGER NOT NULL REFERENCES equipment_booking(booking_id) ON DELETE CASCADE,
                file VARCHAR(100) NOT NULL,
                original_name VARCHAR(255) NOT NULL DEFAULT '',
                created_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        """)


def noop_reverse(apps, schema_editor):
    """No reverse - table may be needed."""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0027_add_booking_result_file"),
    ]

    operations = [
        migrations.RunPython(create_table_if_not_exists, noop_reverse),
    ]
