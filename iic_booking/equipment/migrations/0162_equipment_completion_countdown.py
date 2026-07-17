from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0161_booking_operator_absent_hold_until"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="show_completion_countdown",
            field=models.BooleanField(
                default=False,
                help_text=(
                    'When enabled, booking details show a live countdown to complete the booking. '
                    'The timer starts only after Sample Accepted and uses Completion countdown hours. '
                    'Admin/OIC grace extensions update the deadline.'
                ),
                verbose_name="Show time remaining to complete",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="completion_countdown_hours",
            field=models.PositiveIntegerField(
                default=48,
                help_text=(
                    'Hours allowed to complete the booking after Sample Accepted (used only when '
                    '"Show time remaining to complete" is enabled). Set to 0 to hide the countdown.'
                ),
                verbose_name="Completion countdown hours",
            ),
        ),
    ]
