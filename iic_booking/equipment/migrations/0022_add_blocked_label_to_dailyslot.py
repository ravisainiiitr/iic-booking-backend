# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0021_add_slot_status_choices'),
    ]

    operations = [
        migrations.AddField(
            model_name='dailyslot',
            name='blocked_label',
            field=models.CharField(
                blank=True,
                help_text='Custom label/reason when slot is blocked',
                max_length=255,
                null=True
            ),
        ),
    ]
