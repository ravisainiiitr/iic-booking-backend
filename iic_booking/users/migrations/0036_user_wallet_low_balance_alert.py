# Add wallet low balance alert preference fields to User

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0035_alter_walletrechargerequest_project_details"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="wallet_low_balance_alert_enabled",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, an email is sent daily at 11:00 AM if wallet balance falls below the threshold.",
                verbose_name="Wallet low balance alert enabled",
            ),
        ),
        migrations.AddField(
            model_name="user",
            name="wallet_low_balance_alert_threshold",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Alert is sent when balance is below this amount. Required when low balance alert is enabled.",
                max_digits=10,
                null=True,
                verbose_name="Wallet low balance alert threshold (₹)",
            ),
        ),
    ]
