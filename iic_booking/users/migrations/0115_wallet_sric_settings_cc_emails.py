from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0114_wallet_recharge_request_cashbook_link"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletsricsettings",
            name="project_grant_cc_emails",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Additional addresses copied on Project Grant recharge requests. "
                    "The requesting user is always copied. CC recipients get the request details "
                    "without Approve / Decline links."
                ),
                verbose_name="Project Grant recharge CC email addresses",
            ),
        ),
        migrations.AddField(
            model_name="walletsricsettings",
            name="cash_deposit_cc_emails",
            field=models.TextField(
                blank=True,
                help_text=(
                    "Additional addresses copied on Direct Cash Deposit / Bank Transfer recharge requests. "
                    "The requesting user is always copied. CC recipients get the request details "
                    "without Approve / Decline links."
                ),
                verbose_name="Direct Cash / Bank Transfer recharge CC email addresses",
            ),
        ),
    ]
