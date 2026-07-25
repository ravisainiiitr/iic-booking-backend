from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("equipment", "0173_equipment_atmosphere_sensitive_sample_enabled"),
    ]

    operations = [
        migrations.CreateModel(
            name="LabUserCalendarColorPreference",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "slot_colors",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text="Partial map of slot status keys to hex colours (e.g. AVAILABLE, BOOKED_INTERNAL).",
                        verbose_name="Slot colours",
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lab_user_calendar_color_prefs",
                        to="equipment.equipment",
                        verbose_name="Equipment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="lab_calendar_color_prefs",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Lab user calendar colour preference",
                "verbose_name_plural": "Lab user calendar colour preferences",
            },
        ),
        migrations.AddConstraint(
            model_name="labusercalendarcolorpreference",
            constraint=models.UniqueConstraint(
                fields=("user", "equipment"),
                name="uniq_lab_user_calendar_color_pref",
            ),
        ),
    ]
