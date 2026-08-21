# Additive portal-migration observability, transitions, archive booking table

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0096_portal_migration_legacy_ledger"),
    ]

    operations = [
        migrations.AddField(
            model_name="portalmigrationstate",
            name="incremental_sync_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="last_sync_batch",
            field=models.CharField(blank=True, default="", max_length=64),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="last_sync_duration_ms",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="last_sync_imported_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="last_sync_processed_count",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="sync_runs_total",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="sync_failures_total",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="transactions_imported_total",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="portalmigrationstate",
            name="phase",
            field=models.CharField(
                choices=[
                    ("PREPARATION", "Preparation"),
                    ("PARALLEL_OPERATION", "Parallel operation"),
                    ("FINANCIAL_FREEZE", "Financial freeze"),
                    ("FINAL_SYNC", "Final sync"),
                    ("RECONCILIATION", "Reconciliation"),
                    ("NEW_PORTAL_ACTIVE", "New portal active"),
                    ("OLD_PORTAL_READ_ONLY", "Old portal read-only"),
                    ("OLD_PORTAL_REDIRECT", "Old portal redirect"),
                    ("ARCHIVED", "Archived"),
                ],
                default="PREPARATION",
                max_length=32,
            ),
        ),
        migrations.CreateModel(
            name="PortalMigrationPhaseTransition",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("from_phase", models.CharField(max_length=32)),
                ("to_phase", models.CharField(max_length=32)),
                ("actor_email", models.CharField(blank=True, default="", max_length=255)),
                ("note", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "ordering": ["-created_at"],
                "verbose_name": "Portal migration phase transition",
                "verbose_name_plural": "Portal migration phase transitions",
            },
        ),
        migrations.CreateModel(
            name="LegacyBookingHistoryRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_booking_id", models.PositiveBigIntegerField()),
                ("employee_id", models.CharField(blank=True, default="", max_length=50)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
                ("historical_label", models.CharField(default="Historical / Legacy", max_length=32)),
            ],
            options={
                "verbose_name": "Legacy booking history record",
                "verbose_name_plural": "Legacy booking history records",
            },
        ),
        migrations.AddConstraint(
            model_name="legacybookinghistoryrecord",
            constraint=models.UniqueConstraint(
                fields=("source_booking_id",),
                name="uniq_legacy_booking_source_id",
            ),
        ),
    ]
