from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("equipment", "0187_equipment_pi_and_pi_charge_profile"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="auto_complete_booking",
            field=models.BooleanField(
                default=False,
                help_text=(
                    "When enabled, bookings for this equipment are automatically marked COMPLETED after "
                    "the scheduled end time if meaningful result data exists in the Active folder. "
                    "Does not complete blindly when no result data is present, and never terminates an "
                    "active Remote Analysis session."
                ),
                verbose_name="Auto Complete Booking",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="analysis_data_selection",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text=(
                    "User-confirmed Remote Analysis input selection (source booking, folder, files). "
                    "Recorded before RAA allocation so data choice is not lost while waiting for a PC."
                ),
                verbose_name="Analysis data selection",
            ),
        ),
    ]
