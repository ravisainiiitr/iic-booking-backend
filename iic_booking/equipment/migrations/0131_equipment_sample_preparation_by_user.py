# Sample preparation notice for internal users (per-equipment flag).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0130_quota_check_indexes"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="sample_preparation_by_user",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, internal users (student / individual student / faculty) receive sample "
                    "preparation guidance in booking confirmation and reminder emails. External user emails "
                    "are unchanged."
                ),
                verbose_name="Sample preparation by user (notify internal users in email)",
            ),
        ),
    ]
