# Generated manually for Milestone 13 offline sync / disaster recovery

import django.db.models.deletion
import uuid
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sync", "0007_device_identity_security_m12"),
    ]

    operations = [
        migrations.CreateModel(
            name="AgentRecoveryEvent",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("event_code", models.CharField(db_index=True, max_length=32, verbose_name="Event code")),
                ("component", models.CharField(blank=True, default="", max_length=64, verbose_name="Component")),
                ("from_state", models.CharField(blank=True, default="", max_length=32, verbose_name="From state")),
                ("to_state", models.CharField(blank=True, default="", max_length=32, verbose_name="To state")),
                ("message", models.CharField(max_length=500, verbose_name="Message")),
                ("device_id", models.UUIDField(blank=True, db_index=True, null=True, verbose_name="Device ID")),
                ("agent_uuid", models.UUIDField(blank=True, db_index=True, null=True, verbose_name="Agent UUID")),
                ("correlation_id", models.UUIDField(blank=True, db_index=True, null=True, verbose_name="Correlation ID")),
                ("details", models.JSONField(blank=True, default=dict, verbose_name="Details")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created at")),
                (
                    "sync_agent",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="recovery_events",
                        to="sync.departmentsyncagent",
                        verbose_name="Sync agent",
                    ),
                ),
            ],
            options={
                "verbose_name": "Agent Recovery Event",
                "verbose_name_plural": "Agent Recovery Events",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="AgentConflictResolution",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("conflict_type", models.CharField(db_index=True, max_length=64, verbose_name="Conflict type")),
                ("resolution", models.CharField(max_length=64, verbose_name="Resolution")),
                ("upload_id", models.UUIDField(blank=True, db_index=True, null=True, verbose_name="Upload ID")),
                ("processing_id", models.UUIDField(blank=True, null=True, verbose_name="Processing ID")),
                ("correlation_id", models.UUIDField(blank=True, db_index=True, null=True, verbose_name="Correlation ID")),
                ("details", models.JSONField(blank=True, default=dict, verbose_name="Details")),
                ("created_at", models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="Created at")),
                (
                    "sync_agent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="conflict_resolutions",
                        to="sync.departmentsyncagent",
                        verbose_name="Sync agent",
                    ),
                ),
            ],
            options={
                "verbose_name": "Agent Conflict Resolution",
                "verbose_name_plural": "Agent Conflict Resolutions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="agentrecoveryevent",
            index=models.Index(fields=["event_code", "-created_at"], name="sync_agentr_event_c_m13_idx"),
        ),
        migrations.AddIndex(
            model_name="agentrecoveryevent",
            index=models.Index(fields=["sync_agent", "-created_at"], name="sync_agentr_sync_ag_m13_idx"),
        ),
        migrations.AddIndex(
            model_name="agentconflictresolution",
            index=models.Index(
                fields=["sync_agent", "conflict_type", "-created_at"],
                name="sync_agentc_sync_ag_m13_idx",
            ),
        ),
    ]
