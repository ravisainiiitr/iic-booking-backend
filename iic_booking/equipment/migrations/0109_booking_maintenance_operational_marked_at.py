# Record when equipment became operational for maintenance reschedule window rules.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0108_booking_maintenance_disruption_policy"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="maintenance_operational_marked_at",
            field=models.DateTimeField(
                blank=True,
                help_text="When equipment returned from under maintenance; used with slot window reference to decide whether reschedule calendar gets one or two extra weeks.",
                null=True,
                verbose_name="Equipment operational (maintenance reschedule)",
            ),
        ),
    ]
