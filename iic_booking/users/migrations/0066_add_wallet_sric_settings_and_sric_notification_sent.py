# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0065_rename_users_walletw_user_id_6b678f_idx_users_walle_user_id_7e351e_idx_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletSricSettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "recipient_emails",
                    models.TextField(
                        blank=True,
                        help_text="One address per line, or comma/semicolon separated. Used when a faculty member sends a wallet recharge request to the SRIC Office.",
                        verbose_name="SRIC Office email addresses",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet SRIC office notification settings",
                "verbose_name_plural": "Wallet SRIC office notification settings",
                "db_table": "users_walletsricsettings",
            },
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="sric_notification_sent",
            field=models.BooleanField(
                default=False,
                help_text="Faculty: set when the SRIC Office notification email has been sent for this request.",
                verbose_name="SRIC office notification sent",
            ),
        ),
    ]
