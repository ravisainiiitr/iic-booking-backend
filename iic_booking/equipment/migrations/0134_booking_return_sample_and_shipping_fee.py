# Generated manually for external sample return option + shipping fee snapshot.

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0133_booking_istem_fbr"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="sample_return_after_analysis",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "External bookings only. When enabled, operator should return the submitted sample(s) after analysis. "
                    "A return shipping fee may be added to booking charges."
                ),
                verbose_name="Return sample after analysis",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="return_shipping_fee_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text=(
                    "Snapshot of the return shipping fee applied to this booking (in INR). Stored to keep historical bookings "
                    "stable even if the admin-configured fee changes later."
                ),
                max_digits=10,
                verbose_name="Return shipping fee amount",
            ),
        ),
    ]

