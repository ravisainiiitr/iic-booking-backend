# Generated manually for department internal_grant_code

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0075_department_head"),
    ]

    operations = [
        migrations.AddField(
            model_name="department",
            name="internal_grant_code",
            field=models.CharField(
                blank=True,
                help_text="SRIC / accounts grant code for this internal department (instrument cost centre). Used in SRIC transfer API and recharge workflows.",
                max_length=80,
                verbose_name="Internal grant code",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="utr_reference",
            field=models.CharField(
                blank=True,
                help_text="Bank UTR submitted by user for offline deposit (govt / NEFT)",
                max_length=255,
                verbose_name="UTR / Transfer Reference",
            ),
        ),
    ]
