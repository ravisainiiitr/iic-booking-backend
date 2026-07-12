# Generated manually for maintenance disruption policy fields on Booking.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0107_procurement_workflow_models"),
    ]

    operations = [
        migrations.AddField(
            model_name="booking",
            name="maintenance_disruption_flag",
            field=models.BooleanField(
                default=False,
                help_text="Set when equipment goes under maintenance while the user has an affected same-day booking; cancel stays available; reschedule unlocks when equipment is operational; optional auto-cancel at deadline.",
                verbose_name="Maintenance disruption policy active",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="maintenance_decision_deadline_at",
            field=models.DateTimeField(
                blank=True,
                help_text="If still undecided, booking may be auto-cancelled with full refund (slot window rule).",
                null=True,
                verbose_name="Maintenance policy decision deadline",
            ),
        ),
        migrations.AddField(
            model_name="booking",
            name="maintenance_reschedule_extra_week",
            field=models.BooleanField(
                default=False,
                help_text="When True, slots API extends the visible window by one week for this user reschedule.",
                verbose_name="Extra week for maintenance reschedule",
            ),
        ),
        migrations.AddIndex(
            model_name="booking",
            index=models.Index(
                fields=["maintenance_disruption_flag", "maintenance_decision_deadline_at"],
                name="equipment_b_mainten_6a8b2d_idx",
            ),
        ),
    ]
