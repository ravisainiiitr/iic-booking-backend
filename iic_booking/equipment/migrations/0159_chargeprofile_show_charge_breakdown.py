# Generated manually for ChargeProfile.show_charge_breakdown

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0158_dailyslot_non_home_reservation_semantics"),
    ]

    operations = [
        migrations.AddField(
            model_name="chargeprofile",
            name="show_charge_breakdown",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When enabled, the itemized charge breakdown is shown in the Charge Calculation "
                    "section during booking / estimate. When disabled, only totals are shown."
                ),
                verbose_name="Show charge breakdown",
            ),
        ),
    ]
