from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0070_wallet_credit_facility_celery_beat"),
    ]

    operations = [
        migrations.AddField(
            model_name="walletsricsettings",
            name="grant_code_for_credit",
            field=models.CharField(
                default="IIC-000-002",
                help_text="Shown in the SRIC Office wallet recharge email as “Grant Code for Credit”. Change if your institute uses a different reference.",
                max_length=80,
                verbose_name="Grant code for credit",
            ),
        ),
    ]
