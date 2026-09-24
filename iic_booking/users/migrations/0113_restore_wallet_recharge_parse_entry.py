# Restore WalletRechargeParseEntry (removed in 0092) with credited_to_project_no for cash-book matching.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0112_seed_bill_section_routing_email"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletRechargeParseEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("dated", models.DateField(blank=True, null=True, verbose_name="Date")),
                ("receipt_no", models.CharField(db_index=True, max_length=50, verbose_name="Receipt No.")),
                ("name", models.CharField(blank=True, max_length=255, verbose_name="Name")),
                ("emp_no", models.CharField(db_index=True, max_length=50, verbose_name="Emp No.")),
                ("department", models.CharField(blank=True, max_length=255, verbose_name="Department")),
                ("amount", models.CharField(max_length=50, verbose_name="Amount (display)")),
                ("payment", models.TextField(blank=True, verbose_name="Payment Details")),
                (
                    "credited_to_project_no",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        help_text="Cash-book column: Credited to Project No. (e.g. IIC-000-002). Matched to department_grant_code.",
                        max_length=100,
                        verbose_name="Credited to Project No.",
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                (
                    "source_imap_uid",
                    models.CharField(
                        blank=True,
                        db_index=True,
                        help_text="Mailbox UID of the email this row was imported from (optional).",
                        max_length=32,
                        null=True,
                        verbose_name="Source IMAP message UID",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet Recharge Parse Entry",
                "verbose_name_plural": "Wallet Recharge Parse Entries",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="walletrechargeparseentry",
            constraint=models.UniqueConstraint(
                condition=models.Q(dated__isnull=False),
                fields=("receipt_no", "dated", "emp_no"),
                name="unique_parse_entry_dated",
            ),
        ),
        migrations.AddConstraint(
            model_name="walletrechargeparseentry",
            constraint=models.UniqueConstraint(
                condition=models.Q(dated__isnull=True),
                fields=("receipt_no", "emp_no"),
                name="unique_parse_entry_no_date",
            ),
        ),
    ]
