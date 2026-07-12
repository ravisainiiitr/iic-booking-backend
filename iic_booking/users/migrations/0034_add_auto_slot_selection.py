# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0033_merge_20260217_1606'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='auto_slot_selection',
            field=models.BooleanField(default=False, help_text='If enabled, the system will automatically select the required consecutive slots when booking equipment', verbose_name='Auto Slot Selection'),
        ),
    ]
