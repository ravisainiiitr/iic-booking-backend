from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0172_equipment_slot_tolerance_minutes"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="atmosphere_sensitive_sample_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, bookers and staff may mark a booking as atmosphere-sensitive "
                    "(sample may be submitted at slot start instead of the normal submission lead time). "
                    "When disabled, the option is hidden on booking screens."
                ),
                verbose_name="Allow atmosphere-sensitive sample option",
            ),
        ),
    ]
