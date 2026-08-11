# R6.1 catalog SPA fields (ported; renumbered after R9 0023).

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("remote_analysis", "0023_r9_workstation_data_workspace"),
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
                help_text="Opaque AI-ready metadata blob.",
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="typical_usage",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Short typical-usage blurb shown in Analysis Workspace.",
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="accepted_file_types",
            field=models.JSONField(
                blank=True,
                default=list,
                help_text="File extensions / types this software typically accepts (e.g. ['.raw', '.xy']).",
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="license_server_url",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Optional license server URL / host for network or floating licenses.",
                max_length=512,
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="license_seats",
            field=models.PositiveIntegerField(
                default=0,
                help_text="Configured seats for network/floating/concurrent (0 = use max_concurrent).",
            ),
        ),
        migrations.AddField(
            model_name="analysissoftwarecatalog",
            name="is_archived",
            field=models.BooleanField(
                default=False,
                help_text="Archived catalog entries are hidden from new mappings but retained for history.",
            ),
        ),
        migrations.AlterField(
            model_name="analysissoftwarecatalog",
            name="license_type",
            field=models.CharField(
                blank=True,
                choices=[
                    ("unlimited", "Unlimited"),
                    ("node_locked", "Node Locked"),
                    ("concurrent", "Concurrent"),
                    ("floating", "Floating"),
                    ("network", "Network License Server"),
                    ("dongle", "Dongle"),
                    ("expired", "Expired"),
                    ("other", "Other"),
                ],
                default="",
                help_text="License model for scheduling and seat checks (AI/ops metadata).",
                max_length=128,
            ),
        ),
    ]
