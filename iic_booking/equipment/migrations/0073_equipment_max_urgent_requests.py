# Max urgent requests per equipment (Admin/OIC configurable cap)

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0072_urgentholdexpiryconfig_validity_days"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="max_urgent_requests",
            field=models.PositiveIntegerField(
                blank=True,
                help_text="Maximum number of PENDING urgent requests allowed for this equipment at a time. Configurable by Admin and OIC. Leave empty for no cap.",
                null=True,
                verbose_name="Max urgent requests",
            ),
        ),
    ]
