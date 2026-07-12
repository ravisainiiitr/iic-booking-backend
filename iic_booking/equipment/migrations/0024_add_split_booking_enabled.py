# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0023_add_equipment_group_and_quota'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='split_booking_enabled',
            field=models.BooleanField(default=False, help_text='If enabled, users can select non-consecutive slots, but only when continuous slots are not available', verbose_name='Split Booking Enabled'),
        ),
    ]
