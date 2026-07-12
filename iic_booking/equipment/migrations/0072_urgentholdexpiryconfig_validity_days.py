# Add Urgent booking validity (days) to UrgentHoldExpiryConfig

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0071_equipment_urgent_peak_window_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="urgentholdexpiryconfig",
            name="urgent_booking_validity_days",
            field=models.PositiveIntegerField(
                blank=True,
                default=1,
                help_text="Urgent request is valid for this many days from request creation. After this period, PENDING requests with a hold are auto-expired (hold released, slots freed). Admin and OIC can set this. When set, it overrides Hold expiry (hours).",
                null=True,
                verbose_name="Urgent booking validity (days)",
            ),
        ),
    ]
