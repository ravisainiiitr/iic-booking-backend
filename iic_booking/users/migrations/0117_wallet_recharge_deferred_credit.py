from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0116_wallet_recharge_decline_credit_and_cashbook_mailbox"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletrechargerequest",
            name="wallet_credit_pending",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text=(
                    "Approved by SRIC while the faculty member had a running credit: the wallet is credited "
                    "(and the credit adjusted) only when the SRIC cash-book confirms the funds."
                ),
                verbose_name="Wallet credit awaiting fund receipt",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="wallet_credited_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Wallet credited at"),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="fund_receipt_overdue_days",
            field=models.PositiveSmallIntegerField(
                default=15,
                help_text=(
                    "Approved recharge requests with no matching SRIC cash-book entry after this many days are "
                    "shown to the Main Administrator and Account In-charge every time they open the dashboard."
                ),
                verbose_name="Flag approved requests without a cash-book match after (days)",
            ),
        ),
    ]
