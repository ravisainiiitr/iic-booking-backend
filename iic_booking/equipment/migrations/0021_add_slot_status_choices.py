# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0020_bookingcancellationrequest'),
    ]

    operations = [
        migrations.AlterField(
            model_name='dailyslot',
            name='status',
            field=models.CharField(
                choices=[
                    ('AVAILABLE', 'Available'),
                    ('BOOKED', 'Booked'),
                    ('BLOCKED', 'Blocked'),
                    ('UNDER_MAINTENANCE', 'Under Maintenance'),
                    ('OPERATOR_ABSENT', 'Operator Absent')
                ],
                default='AVAILABLE',
                help_text='Availability status of this slot',
                max_length=20
            ),
        ),
    ]
