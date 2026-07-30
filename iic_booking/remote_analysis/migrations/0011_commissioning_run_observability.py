# Commissioning run observability (engineering support)

import django.db.models.deletion
import uuid
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("remote_analysis", "0010_workspace_lifecycle_phases"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="CommissioningRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("RUNNING", "Running"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                            ("ABORTED", "Aborted"),
                        ],
                        db_index=True,
                        default="RUNNING",
                        max_length=16,
                    ),
                ),
                ("booking_id", models.PositiveIntegerField(blank=True, null=True)),
                ("started_at", models.DateTimeField(auto_now_add=True, db_index=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("summary", models.JSONField(blank=True, default=dict)),
                ("evidence_path", models.CharField(blank=True, default="", max_length=512)),
                ("notes", models.TextField(blank=True, default="")),
                (
                    "operator",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="ra_commissioning_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "workspace",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="commissioning_runs",
                        to="remote_analysis.analysisworkspace",
                    ),
                ),
                (
                    "workstation",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="commissioning_runs",
                        to="remote_analysis.analysisworkstation",
                    ),
                ),
            ],
            options={
                "verbose_name": "Commissioning run",
                "verbose_name_plural": "Commissioning runs",
                "ordering": ["-started_at"],
            },
        ),
        migrations.CreateModel(
            name="CommissioningRunStep",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(db_index=True, max_length=64)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("ended_at", models.DateTimeField(blank=True, null=True)),
                ("duration_ms", models.PositiveIntegerField(blank=True, null=True)),
                ("success", models.BooleanField(blank=True, null=True)),
                ("retry_count", models.PositiveIntegerField(default=0)),
                ("error", models.TextField(blank=True, default="")),
                ("meta", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="steps",
                        to="remote_analysis.commissioningrun",
                    ),
                ),
            ],
            options={
                "ordering": ["created_at"],
                "indexes": [models.Index(fields=["run", "name"], name="remote_anal_run_id_0a2f1e_idx")],
                "unique_together": {("run", "name")},
            },
        ),
        migrations.CreateModel(
            name="CommissioningFailureSnapshot",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("step_name", models.CharField(blank=True, default="", max_length=64)),
                ("captured_at", models.DateTimeField(auto_now_add=True)),
                ("payload", models.JSONField(blank=True, default=dict)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="failure_snapshots",
                        to="remote_analysis.commissioningrun",
                    ),
                ),
            ],
            options={
                "ordering": ["-captured_at"],
            },
        ),
    ]
