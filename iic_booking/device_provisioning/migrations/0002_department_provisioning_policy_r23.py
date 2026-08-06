# Generated manually for Phase R.2.3 — Trusted Department Auto-Approve

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("device_provisioning", "0001_initial_device_provisioning"),
        ("users", "0095_initial_payment_models"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DepartmentProvisioningPolicy",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "provisioning_mode",
                    models.CharField(
                        choices=[
                            ("manual_approval", "Manual Approval"),
                            ("trusted_auto_approve", "Trusted Auto-Approve"),
                            ("restricted_auto_approve", "Restricted Auto-Approve"),
                            ("device_code", "Device Code Approval"),
                        ],
                        db_index=True,
                        default="trusted_auto_approve",
                        max_length=32,
                    ),
                ),
                ("allowed_networks", models.JSONField(blank=True, default=list)),
                ("require_mfa", models.BooleanField(default=False)),
                ("require_device_fingerprint", models.BooleanField(default=True)),
                ("maximum_pending_lifetime_hours", models.PositiveIntegerField(default=24)),
                ("auto_approve_existing_reinstalls", models.BooleanField(default=True)),
                ("audit_enabled", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "department",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="provisioning_policy",
                        to="users.department",
                    ),
                ),
            ],
            options={
                "verbose_name_plural": "Department provisioning policies",
            },
        ),
        migrations.AddField(
            model_name="provisioningsession",
            name="auto_approve_reason",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="provisioningsession",
            name="auto_approved",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="provisioningsession",
            name="device_code",
            field=models.CharField(blank=True, db_index=True, default="", max_length=16),
        ),
        migrations.AddField(
            model_name="provisioningsession",
            name="device_code_expires_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="provisioningsession",
            name="requested_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="requested_provisioning_sessions",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.AlterField(
            model_name="deviceauditlog",
            name="action",
            field=models.CharField(
                choices=[
                    ("created", "Created"),
                    ("approved", "Approved"),
                    ("rejected", "Rejected"),
                    ("provisioned", "Provisioned"),
                    ("reprovisioned", "Reprovisioned"),
                    ("suspended", "Suspended"),
                    ("revoked", "Revoked"),
                    ("retired", "Retired"),
                    ("renamed", "Renamed"),
                    ("assigned", "Assigned"),
                    ("heartbeat", "Heartbeat"),
                    ("policy_updated", "Policy Updated"),
                    ("provisioning_started", "Provisioning Started"),
                    ("auto_approved", "Auto Approved"),
                    ("auto_approve_denied", "Auto Approve Denied"),
                    ("policy_used", "Policy Used"),
                ],
                db_index=True,
                max_length=32,
            ),
        ),
    ]
