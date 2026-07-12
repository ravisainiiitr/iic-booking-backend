# Add user_reply field for booking user reply to reason (Sample Rejected / Held at Office)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0037_rename_hold_at_office_to_held_forwarded"),
    ]

    operations = [
        migrations.AddField(
            model_name="bookingsampletrace",
            name="user_reply",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Booking user reply to the reason (for Sample Rejected or Held at Office)",
                verbose_name="User reply",
            ),
        ),
    ]
