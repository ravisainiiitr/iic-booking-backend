"""Align RemoteDesktopSession.extension_grace_used with production schema.

R9 previously introduced this NOT NULL column on some environments. Master code
later omitted the model field, so Django inserts omitted the column and Postgres
raised: null value in column "extension_grace_used" violates not-null constraint.

This migration is idempotent: it (re)applies a safe DEFAULT/NOT NULL column when
missing, and always registers the field in Django state.
"""

from django.db import migrations, models


def ensure_extension_grace_used(apps, schema_editor):
    table = "remote_analysis_remotedesktopsession"
    column = "extension_grace_used"
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT 1
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = %s
              AND column_name = %s
            """,
            [table, column],
        )
        exists = cursor.fetchone() is not None
        if not exists:
            cursor.execute(
                f"""
                ALTER TABLE {table}
                ADD COLUMN {column} boolean DEFAULT false NOT NULL
                """
            )
            return
        cursor.execute(
            f"""
            ALTER TABLE {table}
            ALTER COLUMN {column} SET DEFAULT false
            """
        )
        cursor.execute(
            f"""
            UPDATE {table}
            SET {column} = false
            WHERE {column} IS NULL
            """
        )
        cursor.execute(
            f"""
            ALTER TABLE {table}
            ALTER COLUMN {column} SET NOT NULL
            """
        )


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0021_retire_direct_rdp_transport"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(ensure_extension_grace_used, migrations.RunPython.noop),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="remotedesktopsession",
                    name="extension_grace_used",
                    field=models.BooleanField(
                        default=False,
                        help_text="True after a one-shot grace extension while others were waiting (R9).",
                    ),
                ),
            ],
        ),
    ]
