# Generated manually for EquipmentSyncTemplate (Phase 1 config push)

import uuid

from django.db import migrations, models
import django.core.validators
import django.db.models.deletion


def default_enabled_features():
    return {
        "watcher": True,
        "upload": True,
        "analysis": False,
        "diagnostics": True,
        "remote_execution": False,
    }


class Migration(migrations.Migration):

    dependencies = [
        ("sync", "0016_dsa_installer_release"),
        ("users", "0001_initial"),
    ]

    operations = [
        migrations.CreateModel(
            name="EquipmentSyncTemplate",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200, verbose_name="Template name")),
                ("code", models.SlugField(max_length=64, unique=True, verbose_name="Template code")),
                ("description", models.TextField(blank=True, default="", verbose_name="Description")),
                ("share_name", models.CharField(blank=True, default="Results", max_length=200, verbose_name="Share name")),
                ("watch_folder", models.CharField(blank=True, default="", max_length=500, verbose_name="Watch folder")),
                (
                    "unc_path_template",
                    models.CharField(
                        blank=True,
                        default="",
                        help_text="Optional; may include {hostname} / {ip} placeholders.",
                        max_length=500,
                        verbose_name="UNC path template",
                    ),
                ),
                (
                    "sync_interval_seconds",
                    models.PositiveIntegerField(
                        default=300,
                        validators=[django.core.validators.MinValueValidator(30)],
                        verbose_name="Synchronization interval (seconds)",
                    ),
                ),
                ("sync_enabled", models.BooleanField(default=True, verbose_name="Sync enabled")),
                ("watch_enabled", models.BooleanField(default=True, verbose_name="Watch folder enabled")),
                ("upload_enabled", models.BooleanField(default=True, verbose_name="Upload enabled")),
                (
                    "enabled_features",
                    models.JSONField(blank=True, default=default_enabled_features, verbose_name="Enabled features"),
                ),
                (
                    "network_mode",
                    models.CharField(
                        choices=[("dhcp", "DHCP (register observed IP)"), ("static", "Static IP (apply when policy allows)")],
                        default="dhcp",
                        help_text="Phase 1: dhcp by default. static only applied when Wizard/DSA policy allows.",
                        max_length=16,
                        verbose_name="Network mode",
                    ),
                ),
                (
                    "windows_account_policy",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Username pattern, password policy refs (no plaintext secrets).",
                        verbose_name="Windows account policy",
                    ),
                ),
                (
                    "folder_layout",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text='e.g. {"raw": "D:\\\\RAW", "results": "D:\\\\RESULTS"}',
                        verbose_name="Folder layout",
                    ),
                ),
                ("firewall_profile", models.JSONField(blank=True, default=dict, verbose_name="Firewall profile")),
                ("retry_policy", models.JSONField(blank=True, default=dict, verbose_name="Retry policy")),
                ("required_software", models.JSONField(blank=True, default=list, verbose_name="Required software list")),
                ("health_thresholds", models.JSONField(blank=True, default=dict, verbose_name="Health thresholds")),
                ("smb_username", models.CharField(blank=True, default="", max_length=200, verbose_name="SMB username hint")),
                (
                    "smb_credential_reference",
                    models.CharField(blank=True, default="", max_length=255, verbose_name="SMB credential reference"),
                ),
                ("is_active", models.BooleanField(default=True, verbose_name="Active")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "department",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="equipment_sync_templates",
                        to="users.department",
                        verbose_name="Department",
                    ),
                ),
            ],
            options={
                "verbose_name": "Equipment Sync Template",
                "verbose_name_plural": "Equipment Sync Templates",
                "ordering": ["name"],
            },
        ),
    ]
