# Generated manually for R6 software-centric catalog metadata.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0021_retire_direct_rdp_transport"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="ai_tags",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="Optional tags for future AI software recommendation (e.g. technique, file type).",
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="ai_metadata",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Opaque AI-ready metadata blob; do not implement consumers in R6.",
            ),
        ),
        migrations.AlterField(
            model_name="analysissoftwarecatalog",
            name="license_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("concurrent", "Concurrent seats"),
                    ("network", "Network / license server"),
                    ("unlimited", "Unlimited"),
                    ("node_locked", "Node-locked"),
                    ("other", "Other"),
                ],
                default="",
                help_text="License model for scheduling and seat checks (AI/ops metadata).",
                max_length=128,
            ),
        ),
    ]
