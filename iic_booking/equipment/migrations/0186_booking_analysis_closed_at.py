from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0185_auto_completion_data_detected_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="analysis_closed_at",
            field=models.DateTimeField(
                blank=True,
                help_text=(
                    "Set when a live remote analysis session ends (user End or timer expiry). "
                    "Blocks starting a new remote analysis session for this booking."
                ),
                null=True,
                verbose_name="Remote analysis closed at",
            ),
        ),
    ]
