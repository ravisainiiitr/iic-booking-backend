from django.db import migrations


def enable_wallet_credit_policy(apps, schema_editor):
    WalletCreditPolicy = apps.get_model("users", "WalletCreditPolicy")
    obj, _ = WalletCreditPolicy.objects.get_or_create(singleton_key="default")
    if not obj.enabled:
        obj.enabled = True
        obj.save(update_fields=["enabled"])


def disable_wallet_credit_policy(apps, schema_editor):
    WalletCreditPolicy = apps.get_model("users", "WalletCreditPolicy")
    WalletCreditPolicy.objects.filter(singleton_key="default").update(enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0108_department_equipment_visibility"),
    ]

    operations = [
        migrations.RunPython(enable_wallet_credit_policy, disable_wallet_credit_policy),
    ]