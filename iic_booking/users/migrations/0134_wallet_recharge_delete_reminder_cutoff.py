import datetime

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0133_wallet_admin_adjustment"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletsricsettings",
            name="cashbook_match_from_date",
            field=models.DateField(
                default=datetime.date(2026, 9, 30),
                help_text=(
                    "Only SRIC cash-book entries whose own date is on or after this date are used to match wallet "
                    "recharge requests (portal launch). Older or undated entries are ignored."
                ),
                verbose_name="Match cash-book entries dated on or after",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="is_deleted",
            field=models.BooleanField(
                db_index=True,
                default=False,
                help_text="Soft-deleted by the Main Administrator: hidden from lists, counts and exports.",
                verbose_name="Deleted",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="deleted_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Deleted at"),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="deleted_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="deleted_wallet_recharge_requests",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Deleted by",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="deletion_reason",
            field=models.TextField(blank=True, verbose_name="Deletion reason"),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="sric_reminder_count",
            field=models.PositiveIntegerField(default=0, verbose_name="SRIC reminders sent"),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="sric_reminder_last_sent_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Last SRIC reminder sent at"),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="sric_reminder_last_sent_by",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="sent_wallet_recharge_sric_reminders",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Last SRIC reminder sent by",
            ),
        ),
    ]
