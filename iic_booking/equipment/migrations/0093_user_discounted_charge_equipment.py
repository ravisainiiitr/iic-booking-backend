from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0092_chargeprofile_pricing_profile_and_seed_discounted"),
        ("users", "0064_add_use_discounted_charge_profile"),
    ]

    operations = [
        migrations.CreateModel(
            name="UserDiscountedChargeEquipment",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("is_active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "equipment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discounted_charge_users",
                        to="equipment.equipment",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="discounted_charge_equipment",
                        to="users.user",
                    ),
                ),
            ],
            options={
                "verbose_name": "User Discounted Charge Equipment",
                "verbose_name_plural": "User Discounted Charge Equipment",
                "ordering": ["user_id", "equipment_id"],
                "unique_together": [("user", "equipment")],
            },
        ),
    ]

