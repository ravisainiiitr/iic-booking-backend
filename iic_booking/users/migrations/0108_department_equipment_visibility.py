# Generated manually for department catalog visibility

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0107_clear_verbose_booking_lock_message"),
    ]

    operations = [
        migrations.AddField(
            model_name="department",
            name="equipment_visibility_enabled",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "Main-administrator master switch: when disabled, equipment belonging to this "
                    "department is hidden from the portal catalog for all users except the main "
                    "administrator, this department's Department Administrator, and Officers-in-Charge "
                    "of that equipment. Defaults to disabled (hidden)."
                ),
                verbose_name="Equipment visibility enabled",
            ),
        ),
    ]
