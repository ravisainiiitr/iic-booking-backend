from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0183_equipment_analysis_raw_results_directories"),
    ]

    operations = [
        migrations.AddField(
            model_name="equipment",
            name="analysis_checkin_minutes",
            field=models.PositiveIntegerField(
                default=10,
                help_text=(
                    "After a workstation is reserved, the user must click Start Analysis Session "
                    "within this many minutes or the reservation is released."
                ),
                verbose_name="Reservation check-in window (minutes)",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="analysis_missed_checkin_policy",
            field=models.CharField(
                choices=[
                    ("END_OF_QUEUE", "Move to end of queue"),
                    ("RETRY_LATER", "Retry allocation later"),
                    ("CANCEL_AFTER_N", "Cancel after N missed check-ins"),
                ],
                default="END_OF_QUEUE",
                max_length=32,
                verbose_name="Missed check-in policy",
            ),
        ),
        migrations.AddField(
            model_name="equipment",
            name="analysis_missed_checkin_limit",
            field=models.PositiveSmallIntegerField(
                default=3,
                help_text="Used when policy is Cancel after N missed check-ins.",
                verbose_name="Missed check-in cancel limit",
            ),
        ),
    ]
