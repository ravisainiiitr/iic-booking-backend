"""
Give database defaults to NOT NULL equipment columns that production gained from branch-only
migrations (0185_r9_extension_grace_minutes, 0188_equipment_auto_complete_booking) but that no
model field maps to. Without a default every INSERT (Add equipment, Duplicate) fails.
Only sets a column default where the column exists; no data is changed.
"""

from django.db import migrations

ORPHAN_DEFAULTS = (
    ("equipment_equipment", "analysis_extension_grace_minutes", "0"),
    ("equipment_equipment", "auto_complete_booking", "false"),
)


def set_defaults(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        for table, column, default in ORPHAN_DEFAULTS:
            cursor.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s",
                [table, column],
            )
            if cursor.fetchone():
                cursor.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default}')


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0207_booking_input_template"),
    ]

    operations = [
        migrations.RunPython(set_defaults, migrations.RunPython.noop),
    ]
