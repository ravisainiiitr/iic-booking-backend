# TA call: expected duty time (from–to), e.g. 9:30 AM to 5:30 PM

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0081_studentequipmentnomination_resume'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipmentoperatingtacall',
            name='expected_duty_time',
            field=models.CharField(
                blank=True,
                help_text='Expected duty time window, e.g. 9:30 AM to 5:30 PM',
                max_length=255,
                verbose_name='Expected duty time (from–to)',
            ),
        ),
    ]
