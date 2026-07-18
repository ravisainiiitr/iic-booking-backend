from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0166_multimode_schedule_display"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="enable_multi_mode",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, this base instrument can have alternate operating modes "
                    "(child equipment) and date-based mode schedules. Default is off; "
                    "equipment behaves as a standard instrument."
                ),
                verbose_name="Enable Multi-Mode Equipment",
            ),
        ),
    ]
