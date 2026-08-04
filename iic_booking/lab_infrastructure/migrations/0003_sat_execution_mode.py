# Lab SAT Execution Mode — evidence, defects, wizard fields

import uuid

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def sat_evidence_upload_to(instance, filename):
    run_id = getattr(instance, "run_id", None) or "unknown"
    return f"sat_evidence/{run_id}/{filename}"


class Migration(migrations.Migration):

    dependencies = [
        ("lab_infrastructure", "0002_sat_test_dashboard"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="sattestcase",
            name="stage",
            field=models.PositiveSmallIntegerField(db_index=True, default=1, help_text="Lab SAT execution stage 1–5"),
        ),
        migrations.AddField(
            model_name="sattestcase",
            name="execution_order",
            field=models.PositiveIntegerField(db_index=True, default=0),
        ),
        migrations.AddField(
            model_name="sattestresult",
            name="administrator_notes",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="sattestresult",
            name="failure_snapshot",
            field=models.JSONField(blank=True, default=dict, help_text="Auto-captured environment at failure time"),
        ),
        migrations.AddField(
            model_name="sattestrun",
            name="lab_context",
            field=models.JSONField(blank=True, default=dict, help_text="Optional building/floor/lab/equipment focus for this run"),
        ),
        migrations.AddField(
            model_name="sattestrun",
            name="readiness_snapshot",
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name="sattestrun",
            name="recommendation",
            field=models.CharField(
                choices=[
                    ("go", "GO"),
                    ("conditional_go", "Conditional GO"),
                    ("no_go", "NO GO"),
                    ("pending", "Pending"),
                ],
                default="pending",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="sattestrun",
            name="current_result",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="+",
                to="lab_infrastructure.sattestresult",
            ),
        ),
        migrations.AlterModelOptions(
            name="sattestcase",
            options={"ordering": ["stage", "execution_order", "test_id"]},
        ),
        migrations.AlterModelOptions(
            name="sattestresult",
            options={"ordering": ["test_case__stage", "test_case__execution_order", "test_case__test_id"]},
        ),
        migrations.AddIndex(
            model_name="sattestcase",
            index=models.Index(fields=["stage", "execution_order"], name="lab_infrast_stage_ord_idx"),
        ),
        migrations.CreateModel(
            name="SatEvidence",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("screenshot", "Screenshot"),
                            ("log", "Log File"),
                            ("config", "Configuration File"),
                            ("network", "Network Capture"),
                            ("video", "Video"),
                            ("other", "Other"),
                        ],
                        default="other",
                        max_length=16,
                    ),
                ),
                ("title", models.CharField(blank=True, default="", max_length=255)),
                ("file", models.FileField(max_length=512, upload_to=sat_evidence_upload_to)),
                ("original_name", models.CharField(blank=True, default="", max_length=255)),
                ("content_type", models.CharField(blank=True, default="", max_length=128)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "result",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="evidence_files",
                        to="lab_infrastructure.sattestresult",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="evidence_files",
                        to="lab_infrastructure.sattestrun",
                    ),
                ),
                (
                    "uploaded_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sat_evidence_uploads",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.CreateModel(
            name="SatDefect",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("bug", "Bug"),
                            ("improvement", "Improvement"),
                            ("configuration", "Configuration Issue"),
                            ("hardware", "Hardware Issue"),
                            ("network", "Network Issue"),
                            ("user_error", "User Error"),
                        ],
                        default="bug",
                        max_length=32,
                    ),
                ),
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
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("open", "Open"),
                            ("in_progress", "In Progress"),
                            ("resolved", "Resolved"),
                            ("wont_fix", "Won't Fix"),
                            ("duplicate", "Duplicate"),
                        ],
                        db_index=True,
                        default="open",
                        max_length=16,
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                ("description", models.TextField(blank=True, default="")),
                ("test_id", models.CharField(blank=True, db_index=True, default="", max_length=32)),
                ("equipment_id", models.CharField(blank=True, default="", max_length=64)),
                ("department_id", models.IntegerField(blank=True, null=True)),
                ("machine_name", models.CharField(blank=True, default="", max_length=255)),
                ("node_id", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("resolved_at", models.DateTimeField(blank=True, null=True)),
                (
                    "created_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="sat_defects_created",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "result",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="defects",
                        to="lab_infrastructure.sattestresult",
                    ),
                ),
                (
                    "run",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="defects",
                        to="lab_infrastructure.sattestrun",
                    ),
                ),
            ],
            options={"ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="satdefect",
            index=models.Index(fields=["status", "severity"], name="lab_infrast_satdef_st_idx"),
        ),
    ]
