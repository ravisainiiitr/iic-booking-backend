# Generated manually for Laboratory Infrastructure Phase 2

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("sync", "0018_equipment_pc_ip_reservation"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ConfigurationChange",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("configuration_version", models.PositiveIntegerField()),
                ("previous_value", models.JSONField(blank=True, default=dict)),
                ("new_value", models.JSONField(blank=True, default=dict)),
                ("reason", models.CharField(blank=True, default="", max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "applied_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="lab_configuration_changes",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "sync_profile",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="configuration_changes",
                        to="sync.equipmentsyncprofile",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="ConfigurationAck",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("equipment_pc_id", models.CharField(blank=True, default="", max_length=64)),
                ("configuration_version", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[("pending", "Pending"), ("applied", "Applied"), ("failed", "Failed")],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("error_message", models.TextField(blank=True, default="")),
                ("applied_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "sync_agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="configuration_acks",
                        to="sync.departmentsyncagent",
                    ),
                ),
                (
                    "sync_profile",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="configuration_acks",
                        to="sync.equipmentsyncprofile",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="LabRepairAction",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("node_kind", models.CharField(max_length=32)),
                ("node_id", models.CharField(max_length=64)),
                ("action", models.CharField(max_length=32)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("queued", "Queued"),
                            ("sent", "Sent"),
                            ("succeeded", "Succeeded"),
                            ("failed", "Failed"),
                        ],
                        default="queued",
                        max_length=16,
                    ),
                ),
                ("result", models.JSONField(blank=True, default=dict)),
                ("error_message", models.TextField(blank=True, default="")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "requested_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="lab_repair_actions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="LabAuditEvent",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("event_type", models.CharField(db_index=True, max_length=64)),
                ("message", models.TextField(blank=True, default="")),
                ("node_kind", models.CharField(blank=True, default="", max_length=32)),
                ("node_id", models.CharField(blank=True, default="", max_length=64)),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("success", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="lab_audit_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="LabAlert",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("code", models.CharField(db_index=True, max_length=64)),
                (
                    "severity",
                    models.CharField(
                        choices=[("warning", "Warning"), ("error", "Error"), ("critical", "Critical")],
                        default="warning",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("acknowledged", "Acknowledged"),
                            ("resolved", "Resolved"),
                        ],
                        default="open",
                        max_length=16,
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                ("detail", models.TextField(blank=True, default="")),
                ("node_kind", models.CharField(blank=True, default="", max_length=32)),
                ("node_id", models.CharField(blank=True, default="", max_length=64)),
                ("department_id", models.IntegerField(blank=True, db_index=True, null=True)),
                ("source", models.CharField(blank=True, default="lab", max_length=32)),
                ("fingerprint", models.CharField(blank=True, db_index=True, default="", max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="configurationchange",
            index=models.Index(fields=["sync_profile", "configuration_version"], name="lab_infra_c_sync_pr_idx"),
        ),
        migrations.AddIndex(
            model_name="configurationack",
            index=models.Index(fields=["configuration_version", "status"], name="lab_infra_c_configu_idx"),
        ),
        migrations.AddIndex(
            model_name="labauditevent",
            index=models.Index(fields=["event_type", "created_at"], name="lab_infra_l_event_t_idx"),
        ),
        migrations.AddIndex(
            model_name="labalert",
            index=models.Index(fields=["status", "severity"], name="lab_infra_l_status_idx"),
        ),
    ]
