# Generated manually for ResultAttachment.s3_key

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("sync", "0014_agent_equipment_replace_laboratory"),
    ]

    operations = [
        migrations.AddField(
            model_name="resultattachment",
            name="s3_key",
            field=models.CharField(
                blank=True,
                default="",
                help_text="When set, file bytes live in S3 (Results/{virtual_booking_id}/...). Local sync_uploads copy may be removed.",
                max_length=1000,
                verbose_name="S3 object key",
            ),
        ),
    ]
