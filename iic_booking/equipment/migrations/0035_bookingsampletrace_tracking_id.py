# Add optional tracking_id to BookingSampleTrace (courier company and tracking ID)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0034_booking_sample_trace"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingsampletrace",
            name="tracking_id",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Optional: Courier company name and tracking ID when status is Sample Sent",
                verbose_name="Tracking ID",
            ),
        ),
    ]
