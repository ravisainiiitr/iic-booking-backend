# Generated manually for Direct Cash Deposit mode + Account In-charge fund receipt verification

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("users", "0092_remove_wallet_recharge_parse_entry"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletsricsettings",
            name="bill_section_emails",
            field=models.TextField(
                blank=True,
                help_text=(
                    "One address per line, or comma/semicolon separated. "
                    "Used for Direct Cash Deposit / Bank Transfer wallet recharge requests. "
                    "Configurable by Main Administrator and Department Administrator."
                ),
                verbose_name="SRIC Bill Section email addresses",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="recharge_mode",
            field=models.CharField(
                choices=[
                    ("project_grant", "Recharge via Project Grant"),
                    ("direct_cash_deposit", "Direct Cash Deposit / Bank Transfer"),
                ],
                db_index=True,
                default="project_grant",
                help_text=(
                    "Offline recharge path: Project Grant (faculty) or Direct Cash Deposit / Bank Transfer "
                    "(all internal users)."
                ),
                max_length=32,
                verbose_name="Recharge Mode",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="undertaking_accepted",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "User acknowledged that Direct Cash Deposit / Bank Transfer is used only when no "
                    "active project grant is available."
                ),
                verbose_name="Undertaking Accepted",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="fund_receipt_verified",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Department Account In-charge confirmed that funds were credited to the department "
                    "grant/account (final financial verification)."
                ),
                verbose_name="Fund Receipt Verified",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="fund_receipt_verified_at",
            field=models.DateTimeField(
                blank=True,
                null=True,
                verbose_name="Fund Receipt Verified At",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="fund_receipt_verification_remarks",
            field=models.TextField(
                blank=True,
                verbose_name="Fund Receipt Verification Remarks",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="fund_receipt_verified_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="wallet_recharge_fund_verifications",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Fund Receipt Verified By",
            ),
        ),
    ]
