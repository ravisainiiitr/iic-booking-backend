# Generated for Phase 8B legacy equipment mapping + booking blocks.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0187_equipment_pi_and_pi_charge_profile"),
        ("users", "0101_migration_booking_settlement"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="portalmigrationstate",
            name="migration_start_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="migration_window_end_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="booking_migration_mode",
            field=models.CharField(
                default="NORMAL",
                help_text="NORMAL|PREPARATION|FREEZE|ACTIVE|SETTLEMENT|COMPLETED",
                max_length=16,
            ),
        ),
        migrations.AddField(
            model_name="portalmigrationstate",
            name="new_portal_url",
            field=models.URLField(blank=True, default=""),
        ),
        migrations.CreateModel(
            name="LegacyBookingMigrationBatch",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("window_start", models.DateTimeField()),
                ("window_end", models.DateTimeField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("DRAFT", "Draft"),
                            ("VALIDATED", "Validated"),
                            ("ARMED", "Armed"),
                            ("ACTIVE", "Active"),
                            ("COMPLETED", "Completed"),
                            ("ABORTED", "Aborted"),
                        ],
                        db_index=True,
                        default="DRAFT",
                        max_length=16,
                    ),
                ),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("counts", models.JSONField(blank=True, default=dict)),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_booking_migration_batches",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Legacy booking migration batch",
                "verbose_name_plural": "Legacy booking migration batches",
                "ordering": ["-started_at"],
            },
        ),
        migrations.CreateModel(
            name="LegacyEquipmentMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("old_equipment_id", models.PositiveBigIntegerField(db_index=True)),
                ("old_equipment_code", models.CharField(blank=True, default="", max_length=64)),
                ("old_equipment_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ACTIVE", "Active"),
                            ("UNMAPPED", "Unmapped"),
                            ("DISABLED", "Disabled"),
                            ("CONFLICT", "Conflict"),
                            ("RETIRED", "Retired"),
                        ],
                        db_index=True,
                        default="UNMAPPED",
                        max_length=16,
                    ),
                ),
                ("mapping_reason", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_equipment_mappings_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_equipment_mappings",
                        to="users.department",
                    ),
                ),
                (
                    "new_equipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_equipment_mappings",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_equipment_mappings_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Legacy equipment mapping",
                "verbose_name_plural": "Legacy equipment mappings",
            },
        ),
        migrations.CreateModel(
            name="LegacyBookingBlock",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("legacy_booking_id", models.PositiveBigIntegerField(db_index=True)),
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                ("source", models.CharField(default="LEGACY_PORTAL", max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("ACTIVE", "Active"),
                            ("RELEASED", "Released"),
                            ("CONFLICT", "Conflict"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        db_index=True,
                        default="ACTIVE",
                        max_length=16,
                    ),
                ),
                ("slot_ids", models.JSONField(blank=True, default=list)),
                ("legacy_payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                ("released_reason", models.CharField(blank=True, default="", max_length=255)),
                (
                    "migration_batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="blocks",
                        to="users.legacybookingmigrationbatch",
                    ),
                ),
                (
                    "new_equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_booking_blocks",
                        to="equipment.equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Legacy booking block",
                "verbose_name_plural": "Legacy booking blocks",
            },
        ),
        migrations.AddConstraint(
            model_name="legacyequipmentmapping",
            constraint=models.UniqueConstraint(fields=("old_equipment_id",), name="uniq_legacy_equipment_old_id"),
        ),
        migrations.AddIndex(
            model_name="legacybookingblock",
            index=models.Index(
                fields=["new_equipment", "status", "start_at", "end_at"],
                name="users_legac_new_equ_8bidx_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="legacybookingblock",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "ACTIVE")),
                fields=("legacy_booking_id", "source"),
                name="uniq_active_legacy_booking_block",
            ),
        ),
    ]
