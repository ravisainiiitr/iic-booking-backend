from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0208_equipment_orphan_column_defaults"),
    ]

    operations = [
        migrations.AlterField(
            model_name="equipment",
            name="sample_submission_lead_hours",
            field=models.PositiveIntegerField(
                default=24,
                help_text=(
                    "Users must submit samples this many hours before the booked slot starts. If that deadline "
                    "falls on a weekend or institute public holiday, it is moved to the previous working day "
                    "(same clock time). Atmosphere-sensitive bookings may submit up to slot start instead. "
                    "Set to 0 for no sample submission deadline (no countdown, reminder email or notification)."
                ),
                verbose_name="Sample submission lead time (hours before slot start)",
            ),
        ),
    ]
