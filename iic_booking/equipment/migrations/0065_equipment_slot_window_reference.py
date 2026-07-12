# Slot window: reference weekday and time when next week becomes visible to internal users

from django.db import migrations, models
import django.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0064_repeat_sample_redesign"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="slot_window_reference_weekday",
            field=models.SmallIntegerField(
                blank=True,
                help_text="Weekday (0=Monday … 6=Sunday) at which the next week becomes visible. Leave empty for no restriction.",
                null=True,
                validators=[
                    django.core.validators.MinValueValidator(0),
                    django.core.validators.MaxValueValidator(6),
                ],
                verbose_name="Slot window reference weekday",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="slot_window_reference_time",
            field=models.TimeField(
                blank=True,
                help_text="Time (24h) on that weekday when the next week opens. Used with slot window reference weekday.",
                null=True,
                verbose_name="Slot window reference time",
            ),
        ),
    ]
