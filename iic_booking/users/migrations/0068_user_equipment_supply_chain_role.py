# Generated manually for equipment supply chain role assignments

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("users", "0067_walletrechargeparseentry_source_imap_uid"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserEquipmentSupplyChainRole",
            fields=[
                ("id", models.AutoField(primary_key=True, serialize=False)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("OFFICE_SUPERINTENDENT", "Office Superintendent"),
                            ("STORE_IN_CHARGE", "Store In Charge"),
                            ("HEAD_OF_DEPARTMENT", "Head of Department"),
                        ],
                        max_length=40,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="equipment_supply_chain_roles",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "User equipment supply chain role",
                "verbose_name_plural": "User equipment supply chain roles",
            },
        ),
        migrations.AddConstraint(
            model_name="userequipmentsupplychainrole",
            constraint=models.UniqueConstraint(fields=("user", "role"), name="uniq_user_equipment_supply_chain_role"),
        ),
    ]
