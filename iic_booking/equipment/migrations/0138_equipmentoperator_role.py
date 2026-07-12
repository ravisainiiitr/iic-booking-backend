from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0137_booking_return_shipping_company"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipmentoperator",
            name="role",
            field=models.CharField(
                choices=[("PRIMARY", "Primary operator"), ("SECONDARY", "Secondary operator")],
                default="PRIMARY",
                help_text="Operator role for this instrument (primary or secondary).",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="equipmentoperator",
            constraint=models.UniqueConstraint(
                fields=("equipment", "role"),
                name="uniq_equipment_operator_role",
            ),
        ),
    ]

