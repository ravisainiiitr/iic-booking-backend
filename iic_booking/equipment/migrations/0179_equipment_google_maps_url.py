from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0178_booking_remote_analysis_integration"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="google_maps_url",
            field=models.URLField(
                blank=True,
                help_text="Optional Google Maps URL for opening the exact lab location",
                null=True,
            ),
        ),
    ]
