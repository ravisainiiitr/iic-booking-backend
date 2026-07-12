# Add display_mode and icon_identifier to Holiday for icon/image in calendar

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0044_holiday_color"),
    ]

    operations = [
        migrations.AddField(
            model_name="holiday",
            name="display_mode",
            field=models.CharField(
                choices=[("color", "Color"), ("image", "Icon/Image")],
                default="color",
                help_text='Weekly calendar: show "Color" as background or "Icon/Image" as background with reason on hover.',
                max_length=10,
                verbose_name="Display as",
            ),
        ),
        migrations.AddField(
            model_name="holiday",
            name="icon_identifier",
            field=models.CharField(
                blank=True,
                help_text='Icon identifier from Iconify (e.g. mdi:holiday). Used when display_mode is "image".',
                max_length=120,
                null=True,
                verbose_name="Icon",
            ),
        ),
    ]
