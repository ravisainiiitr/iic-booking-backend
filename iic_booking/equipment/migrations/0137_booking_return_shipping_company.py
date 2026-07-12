# Generated manually for return-shipping carrier (Accounts workflow).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0136_booking_return_shipping_tracking_fields"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="return_shipping_company",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Courier/shipping company name for return dispatch (set by Accounts In Charge).",
                max_length=128,
                verbose_name="Return shipping company",
            ),
        ),
    ]
