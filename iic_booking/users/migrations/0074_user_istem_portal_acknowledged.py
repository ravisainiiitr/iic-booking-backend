# Generated manually for I-STEM external booking gate.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0073_ensure_walletsricsettings_grant_code_column"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="istem_portal_acknowledged",
            field=models.BooleanField(
                default=False,
                help_text="External users must confirm they have an account on the national I-STEM portal (https://www.istem.gov.in/) before booking equipment.",
                verbose_name="I-STEM portal registration confirmed",
            ),
        ),
    ]
