# Analyze Data — software catalog, equipment mapping, pool, settings

import uuid

import django.db.models.deletion
from django.db import migrations, models
from django.utils.text import slugify


def forwards_backfill_analysis_profile(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    Catalog = apps.get_model("remote_analysis", "AnalysisSoftwareCatalog")
    Mapping = apps.get_model("remote_analysis", "EquipmentAnalysisSoftware")
    SoftwareRequirement = apps.get_model("remote_analysis", "SoftwareRequirement")

    for eq in Equipment.objects.exclude(analysis_profile__isnull=True).exclude(analysis_profile=""):
        profile = (eq.analysis_profile or "").strip()
        if not profile:
            continue
        slug = slugify(profile)[:250] or f"profile-{eq.pk}"
        catalog, created = Catalog.objects.get_or_create(
            slug=slug,
            defaults={
                "id": uuid.uuid4(),
                "name": profile,
                "is_active": True,
            },
        )
        if created or not catalog.software_requirement_id:
            req = SoftwareRequirement.objects.create(
                id=uuid.uuid4(),
                name=f"Catalog: {profile}",
                software=profile,
                required=True,
            )
            catalog.software_requirement = req
            catalog.save(update_fields=["software_requirement"])
        Mapping.objects.get_or_create(
            equipment_id=eq.pk,
            catalog_id=catalog.pk,
            defaults={
                "id": uuid.uuid4(),
                "is_default": True,
                "sort_order": 0,
            },
        )


def backwards_noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0181_waitlistentry_opt_out_and_sample"),
        ("remote_analysis", "0012_single_active_session_per_booking"),
        ("users", "0095_initial_payment_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="AnalysisSoftwareCatalog",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=255)),
                ("slug", models.SlugField(max_length=255, unique=True)),
                ("vendor", models.CharField(blank=True, default="", max_length=255)),
                (
                    "version_constraint",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Minimum / preferred version string matched against inventory.",
                        max_length=128,
                    ),
                ),
                ("license_type", models.CharField(blank=True, default="", max_length=128)),
                (
                    "max_concurrent",
                    models.PositiveIntegerField(
                        default=0,
                        help_text="Max concurrent sessions using this software (0 = unlimited).",
                    ),
                ),
                ("description", models.TextField(blank=True, default="")),
                ("default_session_duration_hours", models.PositiveIntegerField(default=4)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Analysis software catalog",
                "verbose_name_plural": "Analysis software catalog",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="EquipmentAnalysisPool",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("priority_boost", models.IntegerField(default=10)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "verbose_name": "Equipment analysis pool member",
                "verbose_name_plural": "Equipment analysis pool",
                "ordering": ["-priority_boost", "workstation__hostname"],
            },
        ),
        migrations.CreateModel(
            name="EquipmentAnalysisSoftware",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("is_default", models.BooleanField(default=False)),
                ("button_label_override", models.CharField(blank=True, default="", max_length=128)),
                ("sort_order", models.PositiveIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Equipment analysis software",
                "verbose_name_plural": "Equipment analysis software",
                "ordering": ["sort_order", "catalog__name"],
            },
        ),
        migrations.AddField(
            model_name="remoteanalysissettings",
            name="analyze_data_button_label",
            field=models.CharField(
                default="Analyze Data",
                help_text="Default user-facing CTA label on completed bookings.",
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name="remoteanalysissettings",
            name="analyze_data_require_s3_files",
            field=models.BooleanField(
                default=True,
                help_text="When True, Analyze Data requires RAW/results files (S3/DSA/operator) to be present.",
            ),
        ),
        migrations.AddField(
            model_name="remoteanalysissettings",
            name="analyze_data_stage_raw_on_launch",
            field=models.BooleanField(
                default=True,
                help_text="When True, stage booking RAW files into workspace RawData before desktop launch.",
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="software_requirement",
            field=models.OneToOneField(
                blank=True,
                help_text="Linked scheduler profile used by AllocationService.",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="catalog_entry",
                to="remote_analysis.softwarerequirement",
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="supported_departments",
            field=models.ManyToManyField(blank=True, related_name="ra_software_catalog", to="users.department"),
        ),
        migrations.AddField(
            model_name="equipmentanalysispool",
            name="equipment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="analysis_workstation_pool",
                to="equipment.equipment",
            ),
        ),
        migrations.AddField(
            model_name="equipmentanalysispool",
            name="workstation",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="equipment_pools",
                to="remote_analysis.analysisworkstation",
            ),
        ),
        migrations.AddField(
            model_name="equipmentanalysissoftware",
            name="catalog",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="equipment_mappings",
                to="remote_analysis.analysissoftwarecatalog",
            ),
        ),
        migrations.AddField(
            model_name="equipmentanalysissoftware",
            name="equipment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="analysis_software_mappings",
                to="equipment.equipment",
            ),
        ),
        migrations.AddConstraint(
            model_name="equipmentanalysispool",
            constraint=models.UniqueConstraint(fields=("equipment", "workstation"), name="uniq_equipment_analysis_pool"),
        ),
        migrations.AddConstraint(
            model_name="equipmentanalysissoftware",
            constraint=models.UniqueConstraint(fields=("equipment", "catalog"), name="uniq_equipment_analysis_software"),
        ),
        migrations.RunPython(forwards_backfill_analysis_profile, backwards_noop),
    ]
