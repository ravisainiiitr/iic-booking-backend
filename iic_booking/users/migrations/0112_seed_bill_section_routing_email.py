from django.db import migrations


SEED_EMAIL = "ravisaini.15@gmail.com"


def seed_bill_section_email(apps, schema_editor):
    WalletSricSettings = apps.get_model("users", "WalletSricSettings")
    obj, created = WalletSricSettings.objects.get_or_create(
        pk=1,
        defaults={
            "recipient_emails": "",
            "bill_section_emails": SEED_EMAIL,
            "grant_code_for_credit": "IIC-000-002",
        },
    )
    if not created:
        current = (obj.bill_section_emails or "").strip()
        if not current:
            obj.bill_section_emails = SEED_EMAIL
            obj.save(update_fields=["bill_section_emails"])
        elif SEED_EMAIL.lower() not in current.lower():
            # Keep existing addresses; append seed if missing.
            sep = "\n" if "\n" in current else ", "
            obj.bill_section_emails = f"{current.rstrip()}{sep}{SEED_EMAIL}"
            obj.save(update_fields=["bill_section_emails"])


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0111_department_enable_student_wallet_recharge"),
    ]

    operations = [
        migrations.RunPython(seed_bill_section_email, noop_reverse),
    ]
