"""
Repair DBs where migration 0071 is recorded but users_walletsricsettings.grant_code_for_credit
was never created (restore mismatch, failed deploy, etc.).
"""

from django.db import migrations


def ensure_grant_code_column(apps, schema_editor):
    WalletSricSettings = apps.get_model("users", "WalletSricSettings")
    table = WalletSricSettings._meta.db_table
    col = "grant_code_for_credit"
    qn = schema_editor.quote_name
    connection = schema_editor.connection

    with connection.cursor() as cursor:
        if connection.vendor == "postgresql":
            cursor.execute(
                """
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = %s
                  AND column_name = %s
                """,
                [table, col],
            )
            if cursor.fetchone():
                return
            cursor.execute(
                f"ALTER TABLE {qn(table)} ADD COLUMN {qn(col)} varchar(80) NOT NULL DEFAULT %s",
                ["IIC-000-002"],
            )
        elif connection.vendor == "sqlite":
            cursor.execute(f'PRAGMA table_info("{table}")')
            if any(row[1] == col for row in cursor.fetchall()):
                return
            cursor.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{col}" varchar(80) NOT NULL DEFAULT \'IIC-000-002\''
            )
        # Other backends: rely on 0071 having run; no-op here.


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0072_delete_cancelled_wallet_recharge_requests"),
    ]

    operations = [
        migrations.RunPython(ensure_grant_code_column, noop_reverse),
    ]
