# 3D print: materials, STL analysis, PRINT_3D profile type

import uuid
from decimal import Decimal
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import iic_booking.equipment.models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0146_equipment_image_s3_storage"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AlterField(
            model_name="equipment",
            name="profile_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("SAMPLE", "Sample-based"),
                    ("HOUR", "Hour-based"),
                    ("SAMPLE_ELEMENT", "Sample + Element"),
                    ("MULTI_PARAM", "Multi-parameter"),
                    ("PRINT_3D", "3D Print"),
                ],
                help_text="Charge profile type",
                max_length=50,
                null=True,
            ),
        ),
        migrations.CreateModel(
            name="PrintMaterial",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(help_text='Stable code stored in booking input B (e.g. "pla_white")', max_length=64)),
                ("name", models.CharField(help_text="Display name", max_length=255)),
                ("density_g_per_cm3", models.DecimalField(decimal_places=3, default=Decimal("1.240"), help_text="Material density in g/cm³", max_digits=6)),
                ("price_per_gram", models.DecimalField(decimal_places=2, help_text="Charge per gram of filament (INR)", max_digits=10)),
                ("user_type", models.CharField(blank=True, help_text="Optional: limit material to a user type; blank = all types", max_length=50, null=True)),
                ("is_active", models.BooleanField(default=True)),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("equipment", models.ForeignKey(help_text="3D printer equipment this material belongs to", on_delete=django.db.models.deletion.CASCADE, related_name="print_materials", to="equipment.equipment")),
            ],
            options={
                "verbose_name": "3D print material",
                "verbose_name_plural": "3D print materials",
                "ordering": ["equipment", "display_order", "name"],
                "unique_together": {("equipment", "code")},
            },
        ),
        migrations.CreateModel(
            name="PrintAnalysis",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("stl_file", models.FileField(max_length=512, upload_to=iic_booking.equipment.models.print_stl_upload_to)),
                ("original_filename", models.CharField(blank=True, default="", max_length=255)),
                ("status", models.CharField(choices=[("PENDING", "Pending"), ("PROCESSING", "Processing"), ("COMPLETED", "Completed"), ("FAILED", "Failed")], default="PENDING", max_length=20)),
                ("analysis_method", models.CharField(blank=True, choices=[("CURAENGINE", "CuraEngine"), ("HEURISTIC", "Heuristic")], default="", max_length=20)),
                ("weight_grams", models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ("volume_cm3", models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True)),
                ("estimated_time_minutes", models.PositiveIntegerField(blank=True, null=True)),
                ("bounding_box", models.JSONField(blank=True, default=dict)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("error_message", models.TextField(blank=True, default="")),
                ("slicer_settings", models.JSONField(blank=True, default=dict)),
                ("price_per_gram_snapshot", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("material_code_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("booking", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="print_analyses", to="equipment.booking")),
                ("equipment", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="print_analyses", to="equipment.equipment")),
                ("material", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="analyses", to="equipment.printmaterial")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="print_analyses", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "3D print analysis",
                "verbose_name_plural": "3D print analyses",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddField(
            model_name="booking",
            name="print_analysis",
            field=models.ForeignKey(blank=True, help_text="STL analysis snapshot used for 3D print bookings", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="linked_bookings", to="equipment.printanalysis"),
        ),
    ]
