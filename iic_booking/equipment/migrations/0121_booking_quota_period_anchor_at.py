# Generated manually for disruption reschedule quota anchoring

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0120_booking_status_under_maintenance"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="quota_period_anchor_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Quota period anchor datetime",
                help_text=(
                    "When set, weekly/monthly quota calculations use this datetime to decide the quota period "
                    "(instead of the current booking slots). Used for disruption reschedules so quota stays in the original period."
                ),
            ),
        ),
    ]

