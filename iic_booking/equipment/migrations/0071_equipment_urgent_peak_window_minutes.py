# Add urgent_peak_window_minutes to Equipment (Admin/OIC configurable)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0070_internaluserslotwindowsetting"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="urgent_peak_window_minutes",
            field=models.PositiveIntegerField(
                blank=True,
                help_text='For "Unable to get slot despite repeated trials", only failed attempts within this many minutes after the slot window (internal users) time on the reference weekday are shown in the log. Configurable by Admin and OIC. Leave empty to show all non-quota failures in the past 2 weeks.',
                null=True,
                verbose_name="Urgent booking peak window (minutes)",
            ),
        ),
    ]
