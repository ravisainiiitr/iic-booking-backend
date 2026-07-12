# Seed weekend colors (Saturday, Sunday) for calendar weekly view

from django.db import migrations


def seed_weekend_colors(apps, schema_editor):
    CalendarColorSetting = apps.get_model("equipment", "CalendarColorSetting")
    CalendarColorSetting.objects.get_or_create(key="SATURDAY", defaults={"value": "#c7d2fe"})
    CalendarColorSetting.objects.get_or_create(key="SUNDAY", defaults={"value": "#fbcfe8"})


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0052_update_calendar_color_defaults"),
    ]

    operations = [
        migrations.RunPython(seed_weekend_colors, noop_reverse),
    ]
