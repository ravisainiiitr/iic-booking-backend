from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0121_walletsricsettings_project_grant_recharge_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletsricsettings",
            name="direct_cash_recharge_enabled",
            field=models.BooleanField(
                db_default=True,
                default=True,
                help_text="When off, users cannot raise new Direct Cash Deposit / Bank Transfer recharge requests.",
                verbose_name="Allow wallet recharge via Direct Cash Deposit / Bank Transfer",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="online_gateway_recharge_enabled",
            field=models.BooleanField(
                db_default=False,
                default=False,
                help_text="When on, users can recharge a department sub-wallet instantly through Razorpay.",
                verbose_name="Allow wallet recharge via online payment gateway",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="peer_transfer_enabled",
            field=models.BooleanField(
                db_default=True,
                default=True,
                help_text="When off, faculty cannot start new wallet-to-wallet transfers.",
                verbose_name="Allow wallet transfers within the same department",
            ),
        ),
    ]
