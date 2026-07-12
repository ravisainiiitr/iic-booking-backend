# Generated migration for adding expiry_date to Notice model

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('communication', '0004_add_sample_notices'),
    ]

    operations = [
        migrations.AddField(
            model_name='notice',
            name='expiry_date',
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                help_text='Date and time when the notice expires (optional)',
                null=True,
                verbose_name='Expiry Date',
            ),
        ),
    ]
