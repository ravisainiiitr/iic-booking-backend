# Add reason field for Sample Rejected and Hold at Office / Forwarded to Lab

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0035_bookingsampletrace_tracking_id"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    sql="ALTER TABLE equipment_bookingsampletrace ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT '';",
                    reverse_sql="ALTER TABLE equipment_bookingsampletrace DROP COLUMN IF EXISTS reason;",
                ),
            ],
            state_operations=[
                migrations.AddField(
                    model_name="bookingsampletrace",
                    name="reason",
                    field=models.TextField(
                        blank=True,
                        default="",
                        help_text="Mandatory for Sample Rejected and Hold at Office / Forwarded to Lab",
                        verbose_name="Reason",
                    ),
                ),
            ],
        ),
    ]
