"""R9 data-workspace path fields on AnalysisWorkstation (idempotent for prod)."""

from django.db import migrations, models


def ensure_workstation_data_workspace_columns(apps, schema_editor):
    table = "remote_analysis_analysisworkstation"
    columns = {
        "data_root": "varchar(1024) DEFAULT '' NOT NULL",
        "input_path": "varchar(1024) DEFAULT '' NOT NULL",
        "output_path": "varchar(1024) DEFAULT '' NOT NULL",
        "workspace_disk_free_bytes": "bigint NULL",
        "input_bytes": "bigint NULL",
        "output_bytes": "bigint NULL",
        "cleanup_status": "varchar(32) DEFAULT 'idle' NOT NULL",
        "last_sync_at": "timestamp with time zone NULL",
        "disk_low": "boolean DEFAULT false NOT NULL",
    }
    with schema_editor.connection.cursor() as cursor:
        for column, ddl in columns.items():
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
            if cursor.fetchone() is None:
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0022_remotedesktopsession_extension_grace_used"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunPython(
                    ensure_workstation_data_workspace_columns,
                    migrations.RunPython.noop,
                )
            ],
            state_operations=[
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="cleanup_status",
                    field=models.CharField(blank=True, default="idle", max_length=32),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="data_root",
                    field=models.CharField(blank=True, default="", max_length=1024),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="disk_low",
                    field=models.BooleanField(default=False),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="input_bytes",
                    field=models.BigIntegerField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="input_path",
                    field=models.CharField(blank=True, default="", max_length=1024),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="last_sync_at",
                    field=models.DateTimeField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="output_bytes",
                    field=models.BigIntegerField(blank=True, null=True),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="output_path",
                    field=models.CharField(blank=True, default="", max_length=1024),
                ),
                migrations.AddField(
                    model_name="analysisworkstation",
                    name="workspace_disk_free_bytes",
                    field=models.BigIntegerField(blank=True, null=True),
                ),
            ],
        )
    ]
