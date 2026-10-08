"""Per-equipment results overdue time (default 24 hours) and the user-facing results countdown switch (additive)."""

import django.core.validators
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0235_booking_material_charge'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='results_overdue_after_hours',
            field=models.PositiveSmallIntegerField(
                default=24,
                help_text=(
                    'Hours after the later of the booking end and (sample received + booked time) before an open '
                    'booking counts as Results overdue: the overdue counter, the Results overdue list and the daily '
                    '9:00 AM reminder start only then.'
                ),
                validators=[
                    django.core.validators.MinValueValidator(1),
                    django.core.validators.MaxValueValidator(720),
                ],
                verbose_name='Results overdue after (hours)',
            ),
        ),
        migrations.AddField(
            model_name='equipment',
            name='show_results_countdown_to_users',
            field=models.BooleanField(
                default=False,
                help_text=(
                    'When on, the booking details tell the user "Results expected by <date time>" and, once that time '
                    'has passed, "Results overdue by <hours>".'
                ),
                verbose_name='Show results countdown to users',
            ),
        ),
    ]
