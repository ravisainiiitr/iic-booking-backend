# Check-in window fields on AnalysisReservation + status choice expansion

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("remote_analysis", "0019_workstation_machine_fingerprint"),
    ]

    operations = [
        migrations.AddField(
            model_name="analysisreservation",
            name="checkin_expires_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Deadline for user to start the desktop session after reservation.",
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="analysisreservation",
            name="checkin_notified_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="analysisreservation",
            name="missed_checkin_count",
            field=models.PositiveSmallIntegerField(default=0),
        ),
        migrations.AlterField(
            model_name="analysisreservation",
            name="status",
            field=models.CharField(
                choices=[
                    ("REQUESTED", "Requested"),
                    ("VALIDATING", "Validating"),
                    ("QUEUED", "Queued"),
                    ("RESERVED", "Reserved"),
                    ("AWAITING_CHECKIN", "Awaiting user check-in"),
                    ("PREPARING", "Preparing"),
                    ("READY", "Ready"),
                    ("ACTIVE", "Active"),
                    ("COMPLETED", "Completed"),
                    ("EXPIRED", "Expired"),
                    ("CANCELLED", "Cancelled"),
                    ("FAILED", "Failed"),
                ],
                db_index=True,
                default="REQUESTED",
                max_length=32,
            ),
        ),
    ]
