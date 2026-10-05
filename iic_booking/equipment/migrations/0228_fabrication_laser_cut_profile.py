import uuid
from decimal import Decimal

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import iic_booking.equipment.models

PROFILE_TYPE_CHOICES = [
    ("SAMPLE", "Sample-based"),
    ("HOUR", "Hour-based"),
    ("SAMPLE_ELEMENT", "Sample + Element"),
    ("GENERIC", "Generic"),
    ("MULTI_PARAM", "Multi-parameter"),
    ("PRINT_3D", "3D Print"),
    ("LASER_CUT_2D", "2D Laser Cutting"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0227_recurring_slot_block_rule"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="own_material",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="3D printing / 2D laser cutting: the user supplies the material, so the equipment's fixed own-material charge replaces the material cost.",
                verbose_name="User brings own material",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="fabrication_notification_emails",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="For 3D printing and 2D laser cutting equipment: when a booking is confirmed (or its files are replaced), the uploaded design files and booking details are emailed to these addresses.",
                verbose_name="Fabrication file notification emails",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="own_material_fixed_charge",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="For 3D printing and 2D laser cutting equipment: fixed charge (INR, once per booking) that replaces the material cost when the user brings their own material. Leave blank to hide the 'I will bring my own material' option.",
                max_digits=10,
                null=True,
                verbose_name="Own material fixed charge (INR)",
            ),
        ),
        migrations.AlterField(
            model_name="equipment",
            name="print_3d_stl_notification_email",
            field=models.EmailField(
                blank=True,
                default="",
                help_text="Deprecated. Use the fabrication notification email list instead.",
                max_length=254,
                verbose_name="3D print STL notification email (deprecated)",
            ),
        ),
        migrations.AlterField(
            model_name="equipment",
            name="profile_type",
            field=models.CharField(
                blank=True,
                choices=PROFILE_TYPE_CHOICES,
                help_text="Type of equipment profile",
                max_length=20,
                null=True,
            ),
        ),
        migrations.AlterField(
            model_name="chargeprofile",
            name="profile_type",
            field=models.CharField(
                blank=True,
                choices=PROFILE_TYPE_CHOICES,
                help_text="Calculation profile type for this user type (SAMPLE, HOUR, …)",
                max_length=20,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="part_name",
            field=models.CharField(
                blank=True,
                db_default="",
                default="",
                help_text="Part name shown to staff; defaults to the file name.",
                max_length=255,
            ),
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="quantity",
            field=models.PositiveIntegerField(
                db_default=1,
                default=1,
                help_text="Number of copies to print; material and time estimates are multiplied by this.",
            ),
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="superseded_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="printanalysis",
            name="superseded_booking",
            field=models.ForeignKey(
                blank=True,
                help_text="Booking this file belonged to before it was replaced by a re-upload.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="superseded_print_analyses",
                to="equipment.booking",
            ),
        ),
        migrations.AlterField(
            model_name="printanalysis",
            name="price_per_gram_snapshot",
            field=models.DecimalField(blank=True, decimal_places=4, max_digits=12, null=True),
        ),
        migrations.AlterField(
            model_name="printmaterial",
            name="price_per_gram",
            field=models.DecimalField(
                decimal_places=4, help_text="Charge per gram of filament (INR)", max_digits=12
            ),
        ),
        migrations.AddField(
            model_name="printmaterial",
            name="source_rate",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Optional: rate as quoted by the supplier (INR per source unit). When set together with a source unit, the price per gram is calculated from it.",
                max_digits=12,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="printmaterial",
            name="source_unit",
            field=models.CharField(
                blank=True,
                choices=[("PER_KG", "per kg"), ("PER_LITRE", "per litre"), ("PER_GRAM", "per gram")],
                default="",
                help_text="Unit of the source rate (per kg, per litre or per gram).",
                max_length=16,
            ),
        ),
        migrations.CreateModel(
            name="FabricationFileChange",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("changed_at", models.DateTimeField(auto_now_add=True)),
                ("profile_type", models.CharField(choices=PROFILE_TYPE_CHOICES, max_length=20)),
                ("previous_files", models.JSONField(blank=True, default=list)),
                ("new_files", models.JSONField(blank=True, default=list)),
                ("previous_own_material", models.BooleanField(default=False)),
                ("new_own_material", models.BooleanField(default=False)),
                ("charge_before", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                ("charge_after", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                (
                    "reverted_at",
                    models.DateTimeField(
                        blank=True,
                        help_text="Set when an unpaid extra charge window expired and the previous files were restored.",
                        null=True,
                    ),
                ),
                (
                    "booking",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="fabrication_file_changes",
                        to="equipment.booking",
                    ),
                ),
                (
                    "changed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Fabrication file change",
                "verbose_name_plural": "Fabrication file changes",
                "ordering": ["-changed_at"],
            },
        ),
        migrations.CreateModel(
            name="LaserCutBatch",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
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
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="laser_cut_batches",
                        to="equipment.booking",
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="laser_cut_batches",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="laser_cut_batches",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Laser cut upload batch",
                "verbose_name_plural": "Laser cut upload batches",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="LaserSheetMaterial",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("code", models.CharField(help_text='Stable code (e.g. "ms_1mm")', max_length=64)),
                ("name", models.CharField(help_text="Display name", max_length=255)),
                (
                    "material_family",
                    models.CharField(
                        choices=[
                            ("MS", "Mild steel (MS)"),
                            ("SS", "Stainless steel (SS)"),
                            ("ACRYLIC", "Acrylic"),
                            ("MDF", "MDF"),
                            ("OTHER", "Other"),
                        ],
                        default="OTHER",
                        max_length=16,
                    ),
                ),
                ("thickness_mm", models.DecimalField(decimal_places=2, max_digits=6)),
                ("sheet_width_mm", models.DecimalField(decimal_places=1, default=Decimal("2438.4"), max_digits=8)),
                ("sheet_height_mm", models.DecimalField(decimal_places=1, default=Decimal("1219.2"), max_digits=8)),
                (
                    "sheet_rate",
                    models.DecimalField(decimal_places=2, help_text="Price of one full sheet (INR)", max_digits=12),
                ),
                (
                    "user_type",
                    models.CharField(
                        blank=True,
                        help_text="Optional: limit material to a user type; blank = all types",
                        max_length=50,
                        null=True,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("display_order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="laser_sheet_materials",
                        to="equipment.equipment",
                    ),
                ),
            ],
            options={
                "verbose_name": "Laser sheet material",
                "verbose_name_plural": "Laser sheet materials",
                "ordering": ["equipment", "display_order", "name"],
                "unique_together": {("equipment", "code")},
            },
        ),
        migrations.CreateModel(
            name="LaserCutAnalysis",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("sequence", models.PositiveIntegerField(default=0)),
                ("material_code_snapshot", models.CharField(blank=True, default="", max_length=64)),
                ("sheet_rate_snapshot", models.DecimalField(blank=True, decimal_places=2, max_digits=12, null=True)),
                (
                    "dxf_file",
                    models.FileField(max_length=512, upload_to=iic_booking.equipment.models.laser_dxf_upload_to),
                ),
                ("original_filename", models.CharField(blank=True, default="", max_length=255)),
                ("part_name", models.CharField(blank=True, default="", max_length=255)),
                ("quantity", models.PositiveIntegerField(default=1)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("PROCESSING", "Processing"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                        ],
                        default="PENDING",
                        max_length=20,
                    ),
                ),
                (
                    "detected_units",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Drawing units read from the DXF header ($INSUNITS); 'unitless' when not set.",
                        max_length=16,
                    ),
                ),
                (
                    "units",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Units used for sizing (detected, or chosen by the user for unitless drawings).",
                        max_length=16,
                    ),
                ),
                (
                    "units_assumed",
                    models.BooleanField(
                        default=False,
                        help_text="True when the drawing had no units and millimetres were assumed.",
                    ),
                ),
                ("bbox_drawing_units", models.JSONField(blank=True, default=dict)),
                ("width_mm", models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ("height_mm", models.DecimalField(blank=True, decimal_places=3, max_digits=12, null=True)),
                ("area_mm2", models.DecimalField(blank=True, decimal_places=3, max_digits=16, null=True)),
                ("entity_count", models.PositiveIntegerField(default=0)),
                ("warnings", models.JSONField(blank=True, default=list)),
                ("error_message", models.TextField(blank=True, default="")),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("superseded_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="laser_cut_analyses",
                        to="equipment.booking",
                    ),
                ),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="laser_cut_analyses",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "superseded_booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="superseded_laser_cut_analyses",
                        to="equipment.booking",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="laser_cut_analyses",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "batch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="items",
                        to="equipment.lasercutbatch",
                    ),
                ),
                (
                    "material",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="analyses",
                        to="equipment.lasersheetmaterial",
                    ),
                ),
            ],
            options={
                "verbose_name": "Laser cut part",
                "verbose_name_plural": "Laser cut parts",
                "ordering": ["sequence", "created_at"],
            },
        ),
    ]
