# Wallet Recharge Import Record - track IIC accounts file imports to prevent double-credit

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0037_wallet_low_balance_alert_schedule"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletRechargeImportRecord",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("receipt_no", models.CharField(db_index=True, max_length=50, verbose_name="Receipt No.")),
                (
                    "financial_year_start",
                    models.DateField(help_text="Start of financial year (April 1) for this receipt", verbose_name="Financial Year Start"),
                ),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10, verbose_name="Amount")),
                ("dated", models.DateField(blank=True, null=True, verbose_name="Dated")),
                ("received_from_raw", models.TextField(blank=True, verbose_name="Received From (raw)")),
                ("remarks", models.TextField(blank=True, verbose_name="Remarks")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                (
                    "department",
                    models.ForeignKey(
                        limit_choices_to={"department_type": "internal"},
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recharge_import_records",
                        to="users.department",
                        verbose_name="Department (sub-wallet credited)",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="recharge_import_records",
                        to="users.user",
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet Recharge Import Record",
                "verbose_name_plural": "Wallet Recharge Import Records",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="walletrechargeimportrecord",
            constraint=models.UniqueConstraint(
                fields=("receipt_no", "financial_year_start"),
                name="unique_receipt_per_fy",
            ),
        ),
    ]
