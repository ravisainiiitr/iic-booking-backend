"""Equipment Analysis PC RAW/RESULTS directory configuration."""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0182_equipment_analysis_session_duration"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="analysis_raw_data_directory",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Absolute path on Analysis PCs for RAW booking folders "
                    "(e.g. D:\\Analysis\\PXRD\\RAW or \\\\NAS\\share\\RAW). "
                    "When set, the agent creates RAW\\{booking_id} and stages input files there."
                ),
                max_length=1024,
                verbose_name="Analysis raw data directory",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="analysis_results_directory",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Absolute path on Analysis PCs for analyzed/results booking folders "
                    "(e.g. D:\\Analysis\\PXRD\\RESULTS). "
                    "Users should save processed files under RESULTS\\{booking_id}; "
                    "End Analysis uploads from that folder then deletes it."
                ),
                max_length=1024,
                verbose_name="Analysis results directory",
            ),
        ),
    ]
