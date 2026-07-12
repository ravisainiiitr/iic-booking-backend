from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0063_subwallettransaction_related_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="use_discounted_charge_profile",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, the user will use the 'Discounted Charge Profile' for equipment bookings to get ₹0 charges.",
                verbose_name="Use discounted charge profile (waive charges)",
            ),
        ),
    ]

