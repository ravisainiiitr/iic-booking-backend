# Generated manually for Phase 2.5 SAT Test Dashboard

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("lab_infrastructure", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="SatTestCase",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("test_id", models.CharField(db_index=True, max_length=32, unique=True)),
                (
                    "suite",
                    models.CharField(
                        choices=[
                            ("sat", "System Acceptance"),
                            ("uat", "User Acceptance"),
                            ("integration", "Integration"),
                            ("performance", "Performance"),
                            ("security", "Security"),
                        ],
                        db_index=True,
                        default="sat",
                        max_length=16,
                    ),
                ),
                ("module", models.CharField(db_index=True, max_length=64)),
                ("feature", models.CharField(max_length=255)),
                ("preconditions", models.TextField(blank=True, default="")),
                ("steps", models.TextField(blank=True, default="")),
                ("expected_result", models.TextField(blank=True, default="")),
                (
                    "severity",
                    models.CharField(
                        choices=[
                            ("critical", "Critical"),
                            ("high", "High"),
                            ("medium", "Medium"),
                            ("low", "Low"),
                        ],
                        default="high",
                        max_length=16,
                    ),
                ),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["test_id"]},
        ),
        migrations.CreateModel(
            name="SatTestRun",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "suite",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("sat", "System Acceptance"),
                            ("uat", "User Acceptance"),
                            ("integration", "Integration"),
                            ("performance", "Performance"),
                            ("security", "Security"),
                        ],
                        default="",
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("running", "Running"),
                            ("completed", "Completed"),
                            ("aborted", "Aborted"),
                        ],
                        default="running",
                        max_length=16,
                    ),
                ),
                ("notes", models.TextField(blank=True, default="")),
                ("started_at", models.DateTimeField(auto_now_add=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "executed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sat_test_runs",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-started_at"]},
        ),
        migrations.CreateModel(
            name="SatTestResult",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("passed", "Passed"),
                            ("failed", "Failed"),
                            ("skipped", "Skipped"),
                            ("blocked", "Blocked"),
                            ("not_run", "Not Run"),
                        ],
                        db_index=True,
                        default="not_run",
                        max_length=16,
                    ),
                ),
                ("actual_result", models.TextField(blank=True, default="")),
                ("remarks", models.TextField(blank=True, default="")),
                ("log_url", models.URLField(blank=True, default="")),
                ("evidence", models.JSONField(blank=True, default=dict)),
                ("executed_at", models.DateTimeField(blank=True, null=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="results",
                        to="lab_infrastructure.sattestrun",
                    ),
                ),
                (
                    "test_case",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="results",
                        to="lab_infrastructure.sattestcase",
                    ),
                ),
            ],
            options={"ordering": ["test_case__test_id"]},
        ),
        migrations.AddIndex(
            model_name="sattestcase",
            index=models.Index(fields=["suite", "module"], name="lab_infrast_suite_f1a2b3_idx"),
        ),
        migrations.AddIndex(
            model_name="sattestresult",
            index=models.Index(fields=["status", "run"], name="lab_infrast_status_c4d5e6_idx"),
        ),
        migrations.AlterUniqueTogether(
            name="sattestresult",
            unique_together={("run", "test_case")},
        ),
    ]
