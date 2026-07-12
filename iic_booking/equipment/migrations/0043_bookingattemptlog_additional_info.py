# Add additional_info to BookingAttemptLog (input_values, selected_parameters from request)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0042_bookingattemptlog"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingattemptlog",
            name="additional_info",
            field=models.JSONField(
                blank=True,
                help_text="Additional information provided when raising the request (e.g. input_values, selected_parameters)",
                null=True,
            ),
        ),
    ]
