# Generated manually for attachment max_length

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("support", "0005_alter_ticket_ticket_type"),
    ]

    operations = [
        migrations.AlterField(
            model_name="ticket",
            name="attachment",
            field=models.FileField(
                blank=True,
                help_text="Optional document/image attached by the requester",
                max_length=512,
                null=True,
                upload_to="support/ticket_attachments/%Y/%m/%d/",
                verbose_name="Attachment",
            ),
        ),
    ]
