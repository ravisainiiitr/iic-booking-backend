# Add enable_charge_recalculation to Equipment

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0053_weekend_calendar_colors"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="enable_charge_recalculation",
            field=models.BooleanField(
                default=False,
                help_text="When checked, the user can edit any input field on a confirmed (BOOKED) booking before completion, regardless of editing required status. On save, charges are recalculated; if the new charge is higher than the amount already deducted, the difference is debited from the wallet; if lower, the difference is credited. An email notification is sent to the user.",
                verbose_name="Enable charge recalculation",
            ),
        ),
    ]
