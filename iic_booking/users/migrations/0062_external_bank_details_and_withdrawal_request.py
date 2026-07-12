from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0061_alter_externalbillingprofile_id_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="ExternalUserBankDetails",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("account_holder_name", models.CharField(max_length=255, verbose_name="Account Holder Name")),
                ("bank_name", models.CharField(max_length=255, verbose_name="Bank Name")),
                ("account_number", models.CharField(max_length=64, verbose_name="Account Number")),
                ("ifsc_code", models.CharField(max_length=20, verbose_name="IFSC Code")),
                ("branch_name", models.CharField(blank=True, max_length=255, verbose_name="Branch Name")),
                ("account_type", models.CharField(blank=True, max_length=50, verbose_name="Account Type")),
                ("upi_id", models.CharField(blank=True, max_length=255, verbose_name="UPI ID")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="bank_details",
                        to="users.user",
                        verbose_name="User",
                    ),
                ),
            ],
            options={
                "verbose_name": "External User Bank Details",
                "verbose_name_plural": "External User Bank Details",
            },
        ),
        migrations.CreateModel(
            name="WalletWithdrawalRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10, verbose_name="Amount")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("APPROVED", "Approved"),
                            ("REJECTED", "Rejected"),
                            ("CANCELLED", "Cancelled"),
                            ("COMPLETED", "Completed"),
                        ],
                        default="PENDING",
                        max_length=20,
                        verbose_name="Status",
                    ),
                ),
                ("bank_snapshot", models.JSONField(blank=True, default=dict)),
                ("allocations", models.JSONField(blank=True, default=list)),
                ("user_note", models.TextField(blank=True, verbose_name="User Note")),
                ("approved_by_email", models.CharField(blank=True, max_length=255, verbose_name="Approved By Email")),
                ("response_message", models.TextField(blank=True, verbose_name="Response Message")),
                ("utr_reference", models.CharField(blank=True, max_length=255, verbose_name="UTR / Transfer Reference")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                ("responded_at", models.DateTimeField(blank=True, null=True, verbose_name="Responded at")),
                ("completed_at", models.DateTimeField(blank=True, null=True, verbose_name="Completed at")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="wallet_withdrawal_requests",
                        to="users.user",
                        verbose_name="User",
                    ),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="withdrawal_requests",
                        to="users.wallet",
                        verbose_name="Wallet",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet Withdrawal Request",
                "verbose_name_plural": "Wallet Withdrawal Requests",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="walletwithdrawalrequest",
            index=models.Index(fields=["user", "status"], name="users_walletw_user_id_6b678f_idx"),
        ),
        migrations.AddIndex(
            model_name="walletwithdrawalrequest",
            index=models.Index(fields=["status", "created_at"], name="users_walletw_status_439209_idx"),
        ),
    ]

