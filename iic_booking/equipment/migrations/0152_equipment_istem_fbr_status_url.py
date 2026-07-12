from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0151_istem_fbr_charge_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="istem_fbr_status_url",
            field=models.URLField(
                blank=True,
                default="",
                help_text="Optional hyperlink for Officers in Charge / Admins to verify FBR status on I-STEM (separate from the user booking page URL).",
                max_length=500,
                verbose_name="I-STEM FBR status check URL",
            ),
        ),
    ]
