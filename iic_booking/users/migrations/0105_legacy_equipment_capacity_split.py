# Capacity split: 1 legacy equipment calendar → 2 new machines (TG/DTA)

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0187_equipment_pi_and_pi_charge_profile"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("users", "0104_legacy_booking_block_user_mapping"),
    ]

    operations = [
        migrations.CreateModel(
            name="LegacyEquipmentCapacitySplit",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("old_equipment_id", models.PositiveBigIntegerField(db_index=True, unique=True)),
                ("old_equipment_code", models.CharField(blank=True, default="", max_length=64)),
                ("old_equipment_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "policy",
                    models.CharField(
                        choices=[("TIME_BAND_FOLD", "Time-band fold (TG/DTA)")],
                        default="TIME_BAND_FOLD",
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("DRAFT", "Draft"), ("ACTIVE", "Active"), ("DISABLED", "Disabled")],
                        db_index=True,
                        default="DRAFT",
                        max_length=16,
                    ),
                ),
                ("notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_capacity_splits_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "target_a",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_capacity_splits_as_a",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "target_b",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="legacy_capacity_splits_as_b",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "updated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_capacity_splits_updated",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Legacy equipment capacity split",
                "verbose_name_plural": "Legacy equipment capacity splits",
            },
        ),
    ]
