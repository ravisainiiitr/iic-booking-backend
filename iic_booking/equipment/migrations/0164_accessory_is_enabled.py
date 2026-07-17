from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0163_lifecycle_countdowns"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentaccessory",
            name="is_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When disabled, this accessory is hidden from public equipment views. OIC can toggle this.",
                verbose_name="Enabled",
            ),
        ),
        migrations.AddField(
            model_name="equipmentadditionalaccessory",
            name="is_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When disabled, this additional accessory is hidden from public equipment views. OIC can toggle this.",
                verbose_name="Enabled",
            ),
        ),
        migrations.AlterModelOptions(
            name="equipmentaccessory",
            options={
                "verbose_name": "Equipment accessory",
                "verbose_name_plural": "Equipment accessories",
            },
        ),
        migrations.AlterModelOptions(
            name="equipmentadditionalaccessory",
            options={
                "verbose_name": "Equipment additional accessory",
                "verbose_name_plural": "Equipment additional accessories",
            },
        ),
    ]
