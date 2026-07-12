# Generated manually

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('equipment', '0011_bookingevent'),
        ('users', '0020_add_department_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='equipment',
            name='department',
            field=models.ForeignKey(
                blank=True,
                help_text='Department that owns/manages this equipment',
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='equipment',
                to='users.department',
                verbose_name='Department',
            ),
        ),
    ]
