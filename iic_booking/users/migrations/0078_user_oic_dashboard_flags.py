# Generated manually for OIC dashboard feature flags

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0077_payment_gateway_sric_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="oic_enable_ta_nomination",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, this Officer In Charge sees the TA nomination call card on the dashboard.",
                verbose_name="OIC: show TA nomination call",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="oic_enable_ta_duty_assignments",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, this Officer In Charge sees the TA duty assignments card on the dashboard.",
                verbose_name="OIC: show TA duty assignments",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="oic_enable_leave_management",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, this Officer In Charge sees the leave management card on the dashboard.",
                verbose_name="OIC: show leave management",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="oic_enable_reward_config",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, this Officer In Charge sees the reward config card on the dashboard.",
                verbose_name="OIC: show reward config",
            ),
        ),
    ]
