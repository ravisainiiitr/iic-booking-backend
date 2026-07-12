from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0144_booking_virtual_booking_id_max_length"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="completion_email_extra_text",
            field=models.TextField(
                blank=True,
                default="",
                help_text=(
                    "Optional text appended to booking completion emails for this equipment. "
                    "Plain text; URLs (http/https) are turned into clickable links in the HTML email."
                ),
                verbose_name="Extra text for completion emails",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="make",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    'Manufacturer or make (e.g. "Zeiss", "Thermo Fisher"). '
                    "Shown on equipment catalog cards when enabled."
                ),
                max_length=255,
                verbose_name="Make",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="show_make_on_card",
            field=models.BooleanField(
                default=False,
                help_text="When enabled, Make is displayed on catalog cards above department information.",
                verbose_name="Show Make on equipment card",
            ),
        ),
    ]
