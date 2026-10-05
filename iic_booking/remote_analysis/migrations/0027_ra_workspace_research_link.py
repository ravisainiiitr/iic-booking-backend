# Additive only: nullable columns and one new table. Existing rows and tables are untouched.

import uuid

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0026_r13_open_analysis_workspace_cta"),
        ("my_research", "0004_researchfile_origin"),
        ("equipment", "0181_waitlistentry_opt_out_and_sample"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisworkstation",
            name="agent_capabilities",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="analysisworkspace",
            name="research_link",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="analysis_workspaces",
                to="my_research.researchworkspacebooking",
            ),
        ),
        migrations.AddField(
            model_name="analysisworkspace",
            name="transfer_state",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="workspacetransfer",
            name="details",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="BookingAnalysisSetup",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("input_source", models.CharField(blank=True, default="booking", max_length=16)),
                ("state", models.JSONField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "booking",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="analysis_setup",
                        to="equipment.booking",
                    ),
                ),
                (
                    "input_booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to="equipment.booking",
                    ),
                ),
                (
                    "research_link",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="analysis_setups",
                        to="my_research.researchworkspacebooking",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
    ]
