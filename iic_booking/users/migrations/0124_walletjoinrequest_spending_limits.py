from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0123_wallet_recharge_reason_project_closed"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletjoinrequest",
            name="spending_limit_enabled",
            field=models.BooleanField(
                default=False,
                help_text="When on, the student's bookings on the supervisor's wallet are capped by the limits below",
                verbose_name="Spending limit enabled",
            ),
        ),
        migrations.AddField(
            model_name="walletjoinrequest",
            name="weekly_limit_inr",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Maximum the student may charge to the wallet per week (Monday–Sunday, IST)",
                max_digits=12,
                null=True,
                verbose_name="Weekly limit (INR)",
            ),
        ),
        migrations.AddField(
            model_name="walletjoinrequest",
            name="monthly_limit_inr",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Maximum the student may charge to the wallet per calendar month (IST)",
                max_digits=12,
                null=True,
                verbose_name="Monthly limit (INR)",
            ),
        ),
        migrations.AddField(
            model_name="walletjoinrequest",
            name="spending_limit_updated_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Spending limit updated at"),
        ),
    ]
