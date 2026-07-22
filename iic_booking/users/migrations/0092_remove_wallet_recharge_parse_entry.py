# Generated manually — remove Wallet Recharge Parse feature

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0091_department_faculty_credit_facility"),
    ]

    operations = [
        migrations.DeleteModel(
            name="WalletRechargeParseEntry",
        ),
    ]
