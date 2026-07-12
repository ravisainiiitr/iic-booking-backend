# Generated manually for WalletRechargeParseEntry.source_imap_uid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0066_add_wallet_sric_settings_and_sric_notification_sent"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletrechargeparseentry",
            name="source_imap_uid",
            field=models.CharField(
                blank=True,
                db_index=True,
                help_text="Mailbox UID of the email this row was imported from (optional).",
                max_length=32,
                null=True,
                verbose_name="Source IMAP message UID",
            ),
        ),
    ]
