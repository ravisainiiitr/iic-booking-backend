# Add weekly_view_display (TIME | SLOT_ID) to Equipment

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0055_equipment_user_rating_enabled"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="weekly_view_display",
            field=models.CharField(
                choices=[("TIME", "Time (show time on vertical axis)"), ("SLOT_ID", "Slot ID (show slot number/name on vertical axis)")],
                default="TIME",
                help_text="Weekly window vertical axis: Time shows slot times; Slot ID shows slot numbers/names. When Slot ID is chosen, confirmation/reschedule emails send only date and slot ID (no exact timing). Only admin and OIC can change this.",
                max_length=20,
                verbose_name="Weekly view display",
            ),
        ),
    ]
