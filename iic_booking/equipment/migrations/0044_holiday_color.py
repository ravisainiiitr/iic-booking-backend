# Add color to Holiday for calendar display

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0043_bookingattemptlog_additional_info"),
    ]

    operations = [
        migrations.AddField(
            model_name="holiday",
            name="color",
            field=models.CharField(
                blank=True,
                default="#fef3c7",
                help_text="Background color for calendar (e.g. #fef3c7). Used in weekly calendar view.",
                max_length=7,
                null=True,
                verbose_name="Color",
            ),
        ),
    ]
