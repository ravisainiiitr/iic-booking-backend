# Generated manually for wallet transaction filtering (student vs faculty view).

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0062_external_bank_details_and_withdrawal_request"),
    ]

    operations = [
        migrations.AddField(
            model_name="subwallettransaction",
            name="related_user",
            field=models.ForeignKey(
                blank=True,
                help_text="User who initiated or is associated with this transaction (for filtering student view on shared wallets)",
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="related_sub_wallet_transactions",
                to="users.user",
                verbose_name="Related user",
            ),
        ),
    ]
