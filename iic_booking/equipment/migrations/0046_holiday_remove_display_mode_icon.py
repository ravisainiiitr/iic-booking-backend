# Revert: remove display_mode and icon_identifier from Holiday

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0045_holiday_display_mode_icon"),
    ]

    operations = [
        migrations.RemoveField(model_name="holiday", name="display_mode"),
        migrations.RemoveField(model_name="holiday", name="icon_identifier"),
    ]
