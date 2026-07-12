# Calendar color settings for weekly window (admin-configurable)

from django.db import migrations, models


# Defaults matching frontend DEFAULT_SLOT_STATUS_COLORS and holiday default
DEFAULTS = [
    ("AVAILABLE", "#dcfce7"),
    ("BOOKED", "#fecaca"),
    ("BLOCKED", "#e5e7eb"),
    ("UNDER_MAINTENANCE", "#fed7aa"),
    ("OPERATOR_ABSENT", "#fde68a"),
    ("HOLIDAY_DEFAULT", "#fef3c7"),
]


def seed_calendar_colors(apps, schema_editor):
    CalendarColorSetting = apps.get_model("equipment", "CalendarColorSetting")
    for key, value in DEFAULTS:
        CalendarColorSetting.objects.get_or_create(key=key, defaults={"value": value})


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0050_booking_reminder_schedule"),
    ]

    operations = [
        migrations.CreateModel(
            name="CalendarColorSetting",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "key",
                    models.CharField(
                        help_text="e.g. AVAILABLE, BOOKED, BLOCKED, UNDER_MAINTENANCE, OPERATOR_ABSENT, HOLIDAY_DEFAULT",
                        max_length=50,
                        unique=True,
                        verbose_name="Setting key",
                    ),
                ),
                (
                    "value",
                    models.CharField(
                        default="#e5e7eb",
                        help_text="Hex color code (e.g. #dcfce7)",
                        max_length=20,
                        verbose_name="Hex color",
                    ),
                ),
            ],
            options={
                "verbose_name": "Calendar color setting",
                "verbose_name_plural": "Calendar color settings",
                "ordering": ["key"],
            },
        ),
        migrations.RunPython(seed_calendar_colors, noop_reverse),
    ]
