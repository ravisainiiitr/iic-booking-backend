import re

import django.db.models.deletion
from django.db import migrations, models

RECEIPT_RE = re.compile(r"\(Receipt ([^)]+)\)")


def backfill_cashbook_links(apps, schema_editor):
    WalletRechargeRequest = apps.get_model("users", "WalletRechargeRequest")
    WalletRechargeParseEntry = apps.get_model("users", "WalletRechargeParseEntry")

    used_keys = set()
    used_entry_ids = set()
    qs = WalletRechargeRequest.objects.filter(cashbook_receipt_no="").filter(
        models.Q(fund_receipt_verification_remarks__startswith="Auto-verified from SRIC cash-book")
        | models.Q(response_message__startswith="Auto-completed: receipt matched IIC account import")
    ).order_by("created_at")
    for req in qs:
        text = f"{req.fund_receipt_verification_remarks or ''} {req.response_message or ''}"
        m = RECEIPT_RE.search(text)
        if not m:
            continue
        receipt_no = m.group(1).strip()[:50]
        if not receipt_no:
            continue
        entries = list(WalletRechargeParseEntry.objects.filter(receipt_no=receipt_no)[:2])
        entry = entries[0] if len(entries) == 1 else None
        dated = entry.dated if entry else None
        key = (receipt_no, dated)
        if key in used_keys:
            continue
        used_keys.add(key)
        req.cashbook_receipt_no = receipt_no
        req.cashbook_receipt_date = dated
        req.cashbook_matched_at = req.fund_receipt_verified_at or req.responded_at
        fields = ["cashbook_receipt_no", "cashbook_receipt_date", "cashbook_matched_at"]
        if entry is not None and entry.id not in used_entry_ids:
            used_entry_ids.add(entry.id)
            req.cashbook_parse_entry_id = entry.id
            fields.append("cashbook_parse_entry")
        req.save(update_fields=fields)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0113_restore_wallet_recharge_parse_entry"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletrechargerequest",
            name="cashbook_parse_entry",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="matched_recharge_request",
                to="users.walletrechargeparseentry",
                verbose_name="Matched cash-book entry",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="cashbook_receipt_no",
            field=models.CharField(blank=True, db_index=True, max_length=50, verbose_name="Cash-book Receipt No."),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="cashbook_receipt_date",
            field=models.DateField(blank=True, null=True, verbose_name="Cash-book Receipt Date"),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="cashbook_matched_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Cash-book matched at"),
        ),
        migrations.RunPython(backfill_cashbook_links, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="walletrechargerequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("cashbook_receipt_no", ""), _negated=True)
                & models.Q(("cashbook_receipt_date__isnull", False)),
                fields=("cashbook_receipt_no", "cashbook_receipt_date"),
                name="unique_wrr_cashbook_receipt_dated",
            ),
        ),
        migrations.AddConstraint(
            model_name="walletrechargerequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(("cashbook_receipt_no", ""), _negated=True)
                & models.Q(("cashbook_receipt_date__isnull", True)),
                fields=("cashbook_receipt_no",),
                name="unique_wrr_cashbook_receipt_no_date",
            ),
        ),
    ]
