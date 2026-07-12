# Generated manually

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0002_add_user_type"),
    ]

    operations = [
        migrations.CreateModel(
            name="Wallet",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "balance",
                    models.DecimalField(
                        decimal_places=2,
                        default=0.0,
                        help_text="Current wallet balance",
                        max_digits=10,
                        verbose_name="Balance",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Updated at"),
                ),
                (
                    "user",
                    models.OneToOneField(
                        help_text="User who owns this wallet",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wallet",
                        to="users.user",
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet",
                "verbose_name_plural": "Wallets",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="WalletTransaction",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "transaction_type",
                    models.CharField(
                        choices=[("credit", "Credit"), ("debit", "Debit")],
                        max_length=10,
                        verbose_name="Transaction Type",
                    ),
                ),
                (
                    "amount",
                    models.DecimalField(
                        decimal_places=2,
                        help_text="Transaction amount",
                        max_digits=10,
                        verbose_name="Amount",
                    ),
                ),
                (
                    "description",
                    models.TextField(
                        blank=True,
                        help_text="Optional description for the transaction",
                        verbose_name="Description",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Created at"),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="transactions",
                        to="users.wallet",
                        verbose_name="Wallet",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet Transaction",
                "verbose_name_plural": "Wallet Transactions",
                "ordering": ["-created_at"],
            },
        ),
    ]

