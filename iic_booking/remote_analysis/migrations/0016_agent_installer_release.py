# Generated manually for Agent Installer Phase 1 MVP

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0015_reverse_tunnel_transport"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="category",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional grouping for installer software checklist (e.g. XRD, General).",
                max_length=128,
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="icon_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Optional icon URL shown in the agent installer software list.",
            ),
        ),
        migrations.CreateModel(
            name="AgentInstallerRelease",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("version", models.CharField(max_length=64)),
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
                ("min_ram_gb", models.PositiveIntegerField(default=8)),
                ("min_disk_gb", models.PositiveIntegerField(default=20)),
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
                        help_text="Online/setup EXE (self-contained bootstrapper).",
                        max_length=512,
                        null=True,
                        upload_to="ra_agent_installers/%Y/%m/%d/",
                    ),
                ),
                (
                    "offline_file",
                    models.FileField(
                        blank=True,
                        help_text="Optional offline installer package.",
                        max_length=512,
                        null=True,
                        upload_to="ra_agent_installers/%Y/%m/%d/offline/",
                    ),
                ),
                ("original_name", models.CharField(blank=True, default="", max_length=255)),
                ("offline_original_name", models.CharField(blank=True, default="", max_length=255)),
                ("documentation_url", models.URLField(blank=True, default="")),
                ("installation_guide_url", models.URLField(blank=True, default="")),
                ("troubleshooting_guide_url", models.URLField(blank=True, default="")),
                ("is_latest", models.BooleanField(default=False)),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Agent installer release",
                "verbose_name_plural": "Agent installer releases",
                "ordering": ["-release_date", "-created_at"],
            },
        ),
    ]
