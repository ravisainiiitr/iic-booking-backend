"""Mode schedules with blank dates mean "always available"; the per-mode availability setting is retired.

Schema (additive): EquipmentModeSchedule.start_date / end_date become nullable.

Data: a mode still set to "Always available" gets one schedule with blank dates, every day, all day,
"alongside others" (parallel), and becomes "Only on scheduled days". That schedule covers every slot,
and a parallel schedule never blocks the base or other modes, so the mode is bookable exactly as before.
Each conversion is recorded in EquipmentModeAuditLog so the reverse migration can undo it.
"""

from django.db import migrations, models

MIGRATION_TAG = "0232_mode_schedule_optional_dates"
ACTION = "MIGRATION_ALWAYS_TO_OPEN_SCHEDULE"
DATE_HELP = "Leave both dates blank for a schedule with no date limits (the mode is always available)."


def forward(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    Schedule = apps.get_model("equipment", "EquipmentModeSchedule")
    Log = apps.get_model("equipment", "EquipmentModeAuditLog")

    converted = 0
    for mode in Equipment.objects.filter(parent_equipment__isnull=False, mode_availability="ALWAYS").order_by("pk"):
        sched = Schedule.objects.create(
            parent_equipment_id=mode.parent_equipment_id,
            mode_equipment_id=mode.pk,
            start_date=None,
            end_date=None,
            weekdays=[],
            behavior="PARALLEL",
            unavailable_label="Mode not scheduled",
            unavailable_color="#9ca3af",
            exclusive_blocked_label="Alternate mode active",
            exclusive_blocked_color="#9ca3af",
        )
        Equipment.objects.filter(pk=mode.pk).update(mode_availability="SCHEDULED_ONLY")
        Log.objects.create(
            equipment_id=mode.pk,
            equipment_code=mode.code or "",
            action=ACTION,
            details={
                "migration": MIGRATION_TAG,
                "schedule_id": sched.pk,
                "parent_equipment_id": mode.parent_equipment_id,
                "before": {"mode_availability": "ALWAYS"},
                "after": {"mode_availability": "SCHEDULED_ONLY"},
            },
        )
        converted += 1
    print(f"  [{MIGRATION_TAG}] modes converted to an always-available schedule: {converted}")


def backward(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    Schedule = apps.get_model("equipment", "EquipmentModeSchedule")
    Log = apps.get_model("equipment", "EquipmentModeAuditLog")

    rows = Log.objects.filter(action=ACTION, details__migration=MIGRATION_TAG)
    for row in rows.order_by("-id"):
        details = row.details or {}
        if details.get("schedule_id"):
            Schedule.objects.filter(pk=details["schedule_id"]).delete()
        if row.equipment_id:
            Equipment.objects.filter(pk=row.equipment_id).update(mode_availability="ALWAYS")
    rows.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0231_oic_substitute"),
    ]

    operations = [
        migrations.AlterField(
            model_name="equipmentmodeschedule",
            name="start_date",
            field=models.DateField(blank=True, null=True, help_text=DATE_HELP, verbose_name="Start Date"),
        ),
        migrations.AlterField(
            model_name="equipmentmodeschedule",
            name="end_date",
            field=models.DateField(blank=True, null=True, help_text=DATE_HELP, verbose_name="End Date"),
        ),
        migrations.AlterField(
            model_name="equipment",
            name="mode_availability",
            field=models.CharField(
                choices=[("ALWAYS", "Always available"), ("SCHEDULED_ONLY", "Only on scheduled days")],
                default="ALWAYS",
                help_text=(
                    'Only used when this equipment is a mode of a base instrument. Modes linked from the Multi-mode '
                    'page are "Only on scheduled days": bookable while one of their schedules is active, and a schedule '
                    'with blank dates means always available. "Always available" is the legacy setting (bookable '
                    'unless a mutually exclusive schedule of another mode is active).'
                ),
                max_length=20,
                verbose_name="Mode availability",
            ),
        ),
        migrations.RunPython(forward, backward),
    ]
