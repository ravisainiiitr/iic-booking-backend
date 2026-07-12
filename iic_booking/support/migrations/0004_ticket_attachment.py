from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("support", "0003_change_ticket_type_to_charfield"),
    ]

    operations = [
        migrations.AddField(
            model_name="ticket",
            name="attachment",
            field=models.FileField(
                blank=True,
                help_text="Optional document/image attached by the requester",
                null=True,
                upload_to="support/ticket_attachments/%Y/%m/%d/",
                verbose_name="Attachment",
            ),
        ),
    ]

