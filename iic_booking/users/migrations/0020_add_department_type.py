# Generated manually

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0019_userdocument'),
    ]

    operations = [
        migrations.AddField(
            model_name='department',
            name='department_type',
            field=models.CharField(
                choices=[
                    ('internal', 'Internal Department'),
                    ('external', 'External Department'),
                    ('equipment', 'Equipment Department'),
                ],
                default='internal',
                help_text='Type of department: Internal, External, or Equipment',
                max_length=50,
                verbose_name='Department Type',
            ),
        ),
    ]
