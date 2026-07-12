from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0135_seed_external_return_shipping_fee_setting"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="return_shipping_tracking_id",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Optional: Courier company name and tracking ID for returning samples to the user.",
                verbose_name="Return shipping tracking ID",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="return_shipping_tracking_updated_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When the return shipping tracking info was last updated.",
                null=True,
                verbose_name="Return shipping tracking updated at",
            ),
        ),
    ]

