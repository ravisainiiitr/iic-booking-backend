# Common slot window setting for internal users (all equipment)

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0069_equipment_temporary_oic"),
    ]

    operations = [
        migrations.CreateModel(
            name="InternalUserSlotWindowSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "reference_weekday",
                    models.SmallIntegerField(
                        blank=True,
                        help_text="Weekday (0=Monday … 6=Sunday) when the next week becomes visible to internal users.",
                        null=True,
                        validators=[django.core.validators.MinValueValidator(0), django.core.validators.MaxValueValidator(6)],
                        verbose_name="Slot window reference weekday",
                    ),
                ),
                (
                    "reference_time",
                    models.TimeField(
                        blank=True,
                        help_text="Time (24h) on that weekday when the next week opens.",
                        null=True,
                        verbose_name="Slot window reference time",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Internal user slot window setting",
                "verbose_name_plural": "Internal user slot window settings",
            },
        ),
    ]
