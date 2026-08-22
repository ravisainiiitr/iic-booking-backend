# Generated manually for Phase 8A migration refund / settlement authority.

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0187_equipment_pi_and_pi_charge_profile"),
        ("users", "0100_channel_i_gender_enrolment"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="MigrationBookingSettlement",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "legacy_booking_id",
                    models.PositiveBigIntegerField(
                        db_index=True,
                        help_text="Portal booking_id at settlement time (audit copy).",
                    ),
                ),
                (
                    "settlement_type",
                    models.CharField(
                        choices=[("MIGRATION_REFUND", "Migration refund")],
                        db_index=True,
                        default="MIGRATION_REFUND",
                        max_length=32,
                    ),
                ),
                ("original_amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("refund_amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("currency", models.CharField(default="INR", max_length=8)),
                ("reason", models.TextField(blank=True, default="")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                            ("REJECTED", "Rejected"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("processed_by_role", models.CharField(blank=True, default="", max_length=32)),
                ("processed_at", models.DateTimeField(blank=True, null=True)),
                ("reference", models.CharField(blank=True, db_index=True, default="", max_length=64)),
                ("failure_detail", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "booking",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="migration_settlements",
                        to="equipment.booking",
                    ),
                ),
                (
                    "processed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="migration_settlements_processed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="migration_booking_settlements",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "wallet_transaction",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="migration_settlements",
                        to="users.subwallettransaction",
                    ),
                ),
            ],
            options={
                "verbose_name": "Migration booking settlement",
                "verbose_name_plural": "Migration booking settlements",
            },
        ),
        migrations.AddIndex(
            model_name="migrationbookingsettlement",
            index=models.Index(fields=["status", "settlement_type"], name="users_migra_status_0c7f1a_idx"),
        ),
        migrations.AddIndex(
            model_name="migrationbookingsettlement",
            index=models.Index(fields=["legacy_booking_id", "status"], name="users_migra_legacy__b2a1c3_idx"),
        ),
        migrations.AddConstraint(
            model_name="migrationbookingsettlement",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status", "COMPLETED")),
                fields=("booking", "settlement_type"),
                name="uniq_completed_migration_refund_per_booking",
            ),
        ),
    ]
