# Generated manually for Equipment PI + PI Charge Profile

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0186_booking_analysis_closed_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="chargeprofile",
            name="pricing_profile",
            field=models.CharField(
                choices=[
                    ("standard", "Standard Charge Profile"),
                    ("discounted", "Discounted Charge Profile"),
                    ("pi", "PI Charge Profile"),
                ],
                db_index=True,
                default="standard",
                help_text=(
                    "Pricing variant: standard, discounted (zero charges), or PI facility rate."
                ),
                max_length=20,
            ),
        ),
        migrations.CreateModel(
            name="EquipmentPI",
            fields=[
                ("equipment_pi_id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "is_active",
                    models.BooleanField(
                        default=True,
                        help_text="Soft-deactivate without removing the assignment row.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "assigned_by",
                    models.ForeignKey(
                        blank=True,
                        help_text="Admin who assigned this PI.",
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_pis_assigned",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="equipment_pis",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "faculty",
                    models.ForeignKey(
                        help_text="Faculty Principal Investigator for this equipment",
                        limit_choices_to={"user_type": "faculty"},
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="equipment_pi_assignments",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Principal Investigator",
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment PI",
                "verbose_name_plural": "Equipment PIs",
            },
        ),
        migrations.CreateModel(
            name="EquipmentPIAuditLog",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "action",
                    models.CharField(
                        choices=[
                            ("ASSIGNED", "Assigned"),
                            ("REMOVED", "Removed"),
                            ("DEACTIVATED", "Deactivated"),
                            ("REACTIVATED", "Reactivated"),
                            ("CHARGE_PROFILE_UPDATED", "Charge Profile Updated"),
                        ],
                        max_length=32,
                    ),
                ),
                ("details", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="pi_audit_logs",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "faculty",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_pi_audit_logs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "performed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_pi_actions_performed",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment PI Audit Log",
                "verbose_name_plural": "Equipment PI Audit Logs",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="equipmentpi",
            constraint=models.UniqueConstraint(
                fields=("equipment", "faculty"),
                name="uniq_equipment_pi_faculty",
            ),
        ),
        migrations.AddIndex(
            model_name="equipmentpi",
            index=models.Index(
                fields=["equipment", "is_active"],
                name="equipment_e_equipme_pi_act_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="equipmentpi",
            index=models.Index(
                fields=["faculty", "is_active"],
                name="equipment_e_faculty_pi_act_idx",
            ),
        ),
    ]
