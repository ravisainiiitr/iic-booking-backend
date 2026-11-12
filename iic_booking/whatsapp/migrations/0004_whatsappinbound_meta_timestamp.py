from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('whatsapp', '0003_whatsapp_retention_schedule'),
    ]

    operations = [
        migrations.AddField(
            model_name='whatsappinbound',
            name='meta_timestamp',
            field=models.DateTimeField(blank=True, help_text='When Meta says the message was sent. Empty if missing or malformed, and on older rows.', null=True),
        ),
    ]
