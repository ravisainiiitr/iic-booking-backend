# Generated manually for EquipmentAdditionRequest

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0155_dailyslot_home_department_only"),
        ("users", "0076_department_grant_code_payments"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentAdditionRequest",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                ("name", models.CharField(help_text="Proposed equipment name", max_length=255)),
                ("code", models.CharField(help_text="Proposed equipment code", max_length=255)),
                ("description", models.TextField(blank=True, default="")),
                ("make", models.CharField(blank=True, default="", max_length=255)),
                ("model_information", models.CharField(blank=True, default="", max_length=255)),
                ("location", models.TextField(blank=True, default="")),
                ("proposed_oic_name", models.CharField(blank=True, default="", max_length=255)),
                ("proposed_oic_email", models.EmailField(blank=True, default="", max_length=254)),
                ("proposed_operator_name", models.CharField(blank=True, default="", max_length=255)),
                ("proposed_operator_email", models.EmailField(blank=True, default="", max_length=254)),
                ("submitter_name", models.CharField(max_length=255)),
                ("submitter_email", models.EmailField(max_length=254)),
                ("submitter_phone", models.CharField(blank=True, default="", max_length=40)),
                ("notes", models.TextField(blank=True, default="")),
                ("reviewed_at", models.DateTimeField(blank=True, null=True)),
                ("review_notes", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "created_equipment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="addition_requests",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "internal_department",
                    models.ForeignKey(
                        blank=True,
                        limit_choices_to={"department_type": "internal"},
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_addition_requests",
                        to="users.department",
                        verbose_name="Internal department",
                    ),
                ),
                (
                    "reviewed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_addition_reviews",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment addition request",
                "verbose_name_plural": "Equipment addition requests",
                "ordering": ["-created_at"],
            },
        ),
    ]
