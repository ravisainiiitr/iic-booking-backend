# Generated manually for Equipment PC Wizard releases + Deployment Center

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="EquipmentPcWizardRelease",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("product_name", models.CharField(default="Equipment PC Configuration Wizard", max_length=128)),
                ("version", models.CharField(max_length=64)),
                ("build_number", models.CharField(blank=True, default="", max_length=64)),
                (
                    "channel",
                    models.CharField(
                        choices=[("stable", "Stable"), ("rc", "Release Candidate"), ("beta", "Beta")],
                        default="stable",
                        max_length=16,
                    ),
                ),
                ("release_date", models.DateField()),
                ("release_notes", models.TextField(blank=True, default="")),
                (
                    "supported_windows",
                    models.CharField(
                        blank=True,
                        default="Windows 10 Pro, Windows 11 Pro, Windows Server 2019/2022",
                        max_length=255,
                    ),
                ),
                ("download_size_bytes", models.BigIntegerField(default=0)),
                ("sha256", models.CharField(blank=True, default="", max_length=64)),
                (
                    "signature_status",
                    models.CharField(
                        choices=[
                            ("unsigned", "Unsigned"),
                            ("signed", "Signed"),
                            ("verified", "Verified"),
                        ],
                        default="unsigned",
                        max_length=16,
                    ),
                ),
                (
                    "file",
                    models.FileField(
                        blank=True,
                        max_length=512,
                        null=True,
                        upload_to="equipment_pc_wizard/%Y/%m/%d/",
                    ),
                ),
                ("original_name", models.CharField(blank=True, default="", max_length=255)),
                ("documentation_url", models.URLField(blank=True, default="")),
                ("installation_guide_url", models.URLField(blank=True, default="")),
                ("troubleshooting_guide_url", models.URLField(blank=True, default="")),
                ("download_count", models.PositiveIntegerField(default=0)),
                ("is_latest", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Equipment PC Wizard release",
                "verbose_name_plural": "Equipment PC Wizard releases",
                "ordering": ["-release_date", "-created_at"],
            },
        ),
    ]
