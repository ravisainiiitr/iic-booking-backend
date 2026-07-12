# Ensure editing_required column exists on equipment_dynamicinputfield (idempotent)

from django.db import migrations


def add_column_if_not_exists(apps, schema_editor):
    if schema_editor.connection.vendor != "postgresql":
        return
    with schema_editor.connection.cursor() as cursor:
        cursor.execute("""
            ALTER TABLE equipment_dynamicinputfield
            ADD COLUMN IF NOT EXISTS editing_required BOOLEAN NOT NULL DEFAULT FALSE;
        """)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0029_add_editing_required_to_dynamicinputfield"),
    ]

    operations = [
        migrations.RunPython(add_column_if_not_exists, noop_reverse),
    ]
