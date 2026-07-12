# Generated manually for multi-file 3D print ZIP uploads and per-file cancellation.

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0149_printanalysis_actual_weight_time"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PrintAnalysisBatch",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("original_filename", models.CharField(blank=True, default="", max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("PROCESSING", "Processing"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                            ("PARTIAL", "Partial"),
                        ],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                ("slicer_settings", models.JSONField(blank=True, default=dict)),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="print_analysis_batches",
                        to="equipment.booking",
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="print_analysis_batches",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="analysis_batches",
                        to="equipment.printmaterial",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="print_analysis_batches",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "3D print analysis batch",
                "verbose_name_plural": "3D print analysis batches",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="batch",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="items",
                to="equipment.printanalysisbatch",
            ),
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="sequence",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="cancelled_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When set, this file was removed from the booking via partial cancellation.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="print_analysis_batch",
            field=models.ForeignKey(
                blank=True,
                help_text="ZIP batch of STL analyses for multi-file 3D print bookings",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_bookings",
                to="equipment.printanalysisbatch",
            ),
        ),
        migrations.AlterModelOptions(
            name="printanalysis",
            options={
                "ordering": ["sequence", "created_at"],
                "verbose_name": "3D print analysis",
                "verbose_name_plural": "3D print analyses",
            },
        ),
        migrations.AlterField(
            model_name="booking",
            name="print_analysis",
            field=models.ForeignKey(
                blank=True,
                help_text="Primary STL analysis snapshot used for 3D print bookings",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="linked_bookings",
                to="equipment.printanalysis",
            ),
        ),
    ]
