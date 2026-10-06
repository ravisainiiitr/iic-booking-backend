from django.db import migrations


def seed_bill_section_email(apps, schema_editor):
    # Bill Section routing addresses are configured per environment (Main Admin settings); only the row is ensured.
    WalletSricSettings = apps.get_model("users", "WalletSricSettings")
    WalletSricSettings.objects.get_or_create(
        pk=1,
        defaults={
            "recipient_emails": "",
            "bill_section_emails": "",
            "grant_code_for_credit": "IIC-000-002",
        },
    )


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0111_department_enable_student_wallet_recharge"),
    ]

    operations = [
        migrations.RunPython(seed_bill_section_email, noop_reverse),
    ]
