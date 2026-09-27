from decimal import Decimal

from django.db import migrations, models

SCHEDULE_NAME = "Read SRIC cash-book mailbox (every 30 min)"


def create_mailbox_schedule(apps, schema_editor):
    IntervalSchedule = apps.get_model("django_celery_beat", "IntervalSchedule")
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    every_30_minutes, _ = IntervalSchedule.objects.get_or_create(every=30, period="minutes")
    if not PeriodicTask.objects.filter(name=SCHEDULE_NAME).exists():
        PeriodicTask.objects.create(
            name=SCHEDULE_NAME,
            task="users.read_sric_cashbook_mailbox",
            interval=every_30_minutes,
            enabled=True,
        )


def remove_mailbox_schedule(apps, schema_editor):
    PeriodicTask = apps.get_model("django_celery_beat", "PeriodicTask")
    PeriodicTask.objects.filter(name=SCHEDULE_NAME).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0115_wallet_sric_settings_cc_emails"),
        ("django_celery_beat", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletrechargerequest",
            name="decline_credit_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Amount treated as an auto-approved credit facility when SRIC declined this Project Grant request.",
                max_digits=10,
                verbose_name="Auto-approved credit on SRIC decline (₹)",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="decline_credit_outstanding",
            field=models.DecimalField(
                db_index=True,
                decimal_places=2,
                default=Decimal("0.00"),
                help_text="Part of the decline credit not yet recovered from a later approved recharge.",
                max_digits=10,
                verbose_name="Credit outstanding (₹)",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="decline_credit_settled_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Decline credit settled at"),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="credit_settled_amount",
            field=models.DecimalField(
                decimal_places=2,
                default=Decimal("0.00"),
                help_text=(
                    "Portion of this approved recharge used to settle earlier SRIC-declined credits "
                    "on the same wallet and department."
                ),
                max_digits=10,
                verbose_name="Adjusted against outstanding credit (₹)",
            ),
        ),
        migrations.AlterField(
            model_name="walletrechargerequest",
            name="cancellation_source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("user", "Cancelled by User"),
                    ("admin", "Cancelled by Administrator"),
                    ("dept_admin", "Cancelled by Department Administrator"),
                    ("system", "Cancelled by System"),
                    ("sric_declined", "Declined by SRIC (converted to credit)"),
                ],
                help_text="Who cancelled the pending request",
                max_length=20,
                verbose_name="Cancellation Source",
            ),
        ),
        migrations.AlterField(
            model_name="walletrechargerequest",
            name="rejection_reason_code",
            field=models.CharField(
                blank=True,
                choices=[
                    ("wrong_project_grant", "Wrong Project Code"),
                    ("insufficient_balance", "Insufficient Funds in the Project"),
                    ("mismatch_user_info", "Mismatch in User Information"),
                    ("other", "Other"),
                ],
                help_text="Predefined rejection reason when status is Rejected",
                max_length=40,
                verbose_name="Rejection Reason Code",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="ar_sric_emails",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Copied (without Approve / Decline links) on every Project Grant and Direct Cash Deposit "
                    "recharge request and on its final decision."
                ),
                verbose_name="AR SRIC email addresses",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="dean_sric_emails",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Copied (without Approve / Decline links) on every Project Grant recharge request "
                    "and on its final decision."
                ),
                verbose_name="Dean SRIC email addresses",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="decline_converts_to_credit",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When SRIC declines a Project Grant request (before or after approval), the request is "
                    "cancelled and the amount is treated as an auto-approved credit facility, recovered from "
                    "the faculty member's next approved recharge for the same department."
                ),
                verbose_name="Treat SRIC-declined Project Grant requests as auto-approved credit",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="auto_read_cashbook_mailbox",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "Every 30 minutes, read new cash-book emails from the configured senders (IMAP_* server "
                    "settings) and mark matching recharge requests as fund-received."
                ),
                verbose_name="Read the SRIC cash-book mailbox automatically",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="cashbook_sender_emails",
            field=models.TextField(
                blank=True,
                default="bills@sric.iitr.ac.in",
                help_text="Only emails from these senders are read by the automatic cash-book reader.",
                verbose_name="SRIC cash-book sender addresses",
            ),
        ),
        migrations.CreateModel(
            name="WalletCashbookMailboxMessage",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("folder", models.CharField(max_length=120, verbose_name="Folder")),
                ("uid", models.CharField(max_length=32, verbose_name="IMAP UID")),
                ("subject", models.CharField(blank=True, max_length=500, verbose_name="Subject")),
                ("from_addr", models.CharField(blank=True, max_length=255, verbose_name="From")),
                ("attachment_name", models.CharField(blank=True, max_length=255, verbose_name="Attachment")),
                ("rows_parsed", models.PositiveIntegerField(default=0, verbose_name="Rows parsed")),
                ("rows_stored", models.PositiveIntegerField(default=0, verbose_name="Rows stored")),
                ("error", models.TextField(blank=True, verbose_name="Error")),
                ("processed_at", models.DateTimeField(auto_now_add=True, verbose_name="Processed at")),
            ],
            options={
                "verbose_name": "SRIC cash-book mailbox message",
                "verbose_name_plural": "SRIC cash-book mailbox messages",
                "ordering": ["-processed_at"],
                "constraints": [
                    models.UniqueConstraint(fields=("folder", "uid"), name="unique_cashbook_mailbox_folder_uid")
                ],
            },
        ),
        migrations.RunPython(create_mailbox_schedule, remove_mailbox_schedule),
    ]
