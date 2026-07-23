from django.core.validators import MinValueValidator
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0171_external_slot_quota_snapshot_schedule"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="slot_tolerance_minutes",
            field=models.IntegerField(
                default=0,
                help_text=(
                    "Allow analysis time to overrun allocated slot capacity by up to this many minutes "
                    "before another slot is required. Formula: slots = ceil((analysis_time − tolerance) / slot_duration), "
                    "minimum 1. 0 preserves legacy strict ceil(analysis_time / slot_duration) behaviour. "
                    "Configurable by Main Administrator and Department Administrator."
                ),
                validators=[MinValueValidator(0)],
                verbose_name="Slot Tolerance (Minutes)",
            ),
        ),
    ]
