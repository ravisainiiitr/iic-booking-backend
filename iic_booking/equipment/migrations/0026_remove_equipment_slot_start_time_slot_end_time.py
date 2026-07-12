# Generated manually - timing is per Slot Masters (open_time/close_time), not equipment-level

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0025_equipment_slot_start_time_slot_end_time'),
    ]

    operations = [
        migrations.RemoveField(model_name='equipment', name='slot_start_time'),
        migrations.RemoveField(model_name='equipment', name='slot_end_time'),
    ]
