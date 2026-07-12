# Booking: pending amount after charge recalculation (negative = refund, positive = extra to pay)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0057_equipment_waitlist_queue_depth_and_waitlistentry"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="charge_recalculation_pending_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="After charge recalculation: negative = refund to process, positive = extra amount to pay. Cleared when Refund or Pay Now is completed.",
                max_digits=10,
                null=True,
                verbose_name="Charge recalculation pending amount",
            ),
        ),
    ]
