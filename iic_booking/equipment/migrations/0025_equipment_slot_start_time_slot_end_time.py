# Generated manually

import datetime
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0024_add_split_booking_enabled'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='slot_start_time',
            field=models.TimeField(
                default=datetime.time(9, 0),
                help_text='Start time for the slot window (e.g. 9:00 AM). Used when generating slot masters.',
                verbose_name='Slot window start time',
            ),
            preserve_default=True,
        ),
        migrations.AddField(
            model_name='equipment',
            name='slot_end_time',
            field=models.TimeField(
                default=datetime.time(18, 0),
                help_text='End time for the slot window (e.g. 6:00 PM). Used when generating slot masters.',
                verbose_name='Slot window end time',
            ),
            preserve_default=True,
        ),
    ]
