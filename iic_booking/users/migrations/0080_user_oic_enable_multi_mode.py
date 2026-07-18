# Generated manually for OIC multi-mode enable flag

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0079_sric_grant_code_help_text"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="oic_enable_multi_mode",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, this Officer In Charge can configure Multi-Mode Equipment "
                    "(parent/child modes and schedules). When disabled (default), all equipment "
                    "they manage behave as standard standalone (parent) equipment."
                ),
                verbose_name="OIC: enable Multi-Mode Equipment",
            ),
        ),
    ]
