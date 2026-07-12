# Add user_rating_enabled to Equipment (enable/disable user rating per equipment; admin and OIC only)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0054_equipment_enable_charge_recalculation"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="user_rating_enabled",
            field=models.BooleanField(
                default=True,
                help_text="When unchecked, users cannot submit a star rating or feedback for completed bookings of this equipment. Only admin and OIC can change this setting.",
                verbose_name="User rating enabled",
            ),
        ),
    ]
