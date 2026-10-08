"""DailySlot.released_by_booking_at: only slots given back by a booking cancellation / reschedule auto-confirm the
waitlist (additive, nullable)."""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0238_urgent_request_without_slots'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailyslot',
            name='released_by_booking_at',
            field=models.DateTimeField(
                blank=True,
                null=True,
                help_text=(
                    'When a booking cancellation or reschedule last gave this slot back. Any other status change '
                    '(OIC / admin, maintenance, rules) clears it. Only such slots auto-confirm waitlisted users.'
                ),
            ),
        ),
    ]
