from django.db import migrations


def delete_cancelled_recharge_requests(apps, schema_editor):
    WalletRechargeRequest = apps.get_model("users", "WalletRechargeRequest")
    WalletRechargeRequest.objects.filter(status="CANCELLED").delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0071_walletsricsettings_grant_code_for_credit"),
    ]

    operations = [
        migrations.RunPython(delete_cancelled_recharge_requests, noop_reverse),
    ]
