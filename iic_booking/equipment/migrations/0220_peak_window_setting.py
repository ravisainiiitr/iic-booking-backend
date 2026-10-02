import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0219_equipment_allow_multiple_sample_sets"),
    ]

    operations = [
        migrations.CreateModel(
            name="PeakWindowSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Turn the peak booking window on or off for the whole portal.",
                        verbose_name="Peak window enabled",
                    ),
                ),
                (
                    "lead_minutes",
                    models.PositiveSmallIntegerField(
                        default=5,
                        help_text="The peak window starts this many minutes before the slot opening time.",
                        validators=[django.core.validators.MaxValueValidator(120)],
                        verbose_name="Minutes before opening",
                    ),
                ),
                (
                    "trail_minutes",
                    models.PositiveSmallIntegerField(
                        default=15,
                        help_text="The peak window ends this many minutes after the slot opening time.",
                        validators=[django.core.validators.MaxValueValidator(240)],
                        verbose_name="Minutes after opening",
                    ),
                ),
                (
                    "block_external_users",
                    models.BooleanField(
                        default=True,
                        help_text=(
                            "External, Industry, R&D and other non-IITR users cannot sign in or use the portal "
                            "during the peak window. Admins, Officers in Charge and staff are never paused."
                        ),
                        verbose_name="Pause external users during the window",
                    ),
                ),
                (
                    "external_notice_minutes",
                    models.PositiveSmallIntegerField(
                        default=30,
                        help_text="Show external users an advance notice banner this many minutes before the window.",
                        validators=[django.core.validators.MaxValueValidator(240)],
                        verbose_name="External notice (minutes before the window)",
                    ),
                ),
                (
                    "defer_background_tasks",
                    models.BooleanField(
                        default=True,
                        help_text="Delay reports, bulk emails, digests, re-indexing and housekeeping until the window ends.",
                        verbose_name="Defer background work during the window",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Peak booking window setting",
                "verbose_name_plural": "Peak booking window settings",
            },
        ),
    ]
