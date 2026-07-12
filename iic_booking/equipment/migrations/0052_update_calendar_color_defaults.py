# Update calendar color settings to more pronounced defaults for weekly window visibility

from django.db import migrations


# Pronounced defaults (match api_views.DEFAULT_CALENDAR_COLORS)
UPDATES = [
    ("AVAILABLE", "#22c55e"),
    ("BOOKED", "#ef4444"),
    ("BLOCKED", "#64748b"),
    ("UNDER_MAINTENANCE", "#f97316"),
    ("OPERATOR_ABSENT", "#eab308"),
    ("HOLIDAY_DEFAULT", "#f59e0b"),
]


def update_colors(apps, schema_editor):
    CalendarColorSetting = apps.get_model("equipment", "CalendarColorSetting")
    for key, value in UPDATES:
        CalendarColorSetting.objects.filter(key=key).update(value=value)


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0051_calendarcolorsetting"),
    ]

    operations = [
        migrations.RunPython(update_colors, noop_reverse),
    ]
