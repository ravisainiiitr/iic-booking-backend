# Per-equipment plain text appended to booking confirmation and reminder emails.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0131_equipment_sample_preparation_by_user"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="booking_email_extra_text",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Optional plain text appended after the standard booking message in booking confirmation emails "
                    "(and in same-day reminder emails) for this equipment. Leave empty for no extra text."
                ),
                verbose_name="Extra text for booking emails (plain text)",
            ),
        ),
    ]
