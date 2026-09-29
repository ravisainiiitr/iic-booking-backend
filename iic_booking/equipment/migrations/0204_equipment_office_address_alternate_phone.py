from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0203_calendar_feed_token"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="office_address",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Optional office address for enquiries about this equipment",
                verbose_name="Office address",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="alternate_phone_number",
            field=models.CharField(
                blank=True, default="", max_length=40, verbose_name="Alternate phone number"
            ),
        ),
    ]
