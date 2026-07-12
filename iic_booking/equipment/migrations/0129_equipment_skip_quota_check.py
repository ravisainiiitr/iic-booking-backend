from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0128_equipment_lifecycle_supply_chain"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="skip_quota_check",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, weekly/monthly quota checks are skipped for this equipment only "
                    "(booking, reschedule, and waitlist auto-book). Global SKIP_BOOKING_QUOTA_CHECK still applies site-wide."
                ),
                verbose_name="Skip quota check for this equipment",
            ),
        ),
    ]
