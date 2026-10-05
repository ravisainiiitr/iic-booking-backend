"""Audited multi-mode cleanup.

* Modes (equipment with a parent) are never flagged as multi-mode bases: clear enable_multi_mode.
* Modes that already have at least one schedule keep today's behaviour ("Only on scheduled days");
  modes without schedules become "Always available".
* A base is flagged exactly when it has at least one mode.

Parent links and schedules are kept. Every change is recorded in EquipmentModeAuditLog so the
reverse migration can restore the previous flags.
"""

from django.db import migrations

MIGRATION_TAG = "0226_multimode_data_cleanup"
ACTION_MODE = "MIGRATION_MODE_CLEANUP"
ACTION_BASE = "MIGRATION_BASE_FLAG"


def _log(Log, eq, action, before, after, extra=None):
    details = {"migration": MIGRATION_TAG, "before": before, "after": after}
    if extra:
        details.update(extra)
    Log.objects.create(equipment_id=eq.pk, equipment_code=eq.code or "", action=action, details=details)
    print(f"  [{MIGRATION_TAG}] {action} {eq.code}: {before} -> {after}")


def forward(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    Schedule = apps.get_model("equipment", "EquipmentModeSchedule")
    Log = apps.get_model("equipment", "EquipmentModeAuditLog")

    scheduled_mode_ids = set(Schedule.objects.values_list("mode_equipment_id", flat=True))
    for mode in Equipment.objects.filter(parent_equipment__isnull=False).order_by("pk"):
        availability = "SCHEDULED_ONLY" if mode.pk in scheduled_mode_ids else "ALWAYS"
        before = {"enable_multi_mode": mode.enable_multi_mode, "mode_availability": mode.mode_availability}
        after = {"enable_multi_mode": False, "mode_availability": availability}
        if before == after:
            continue
        Equipment.objects.filter(pk=mode.pk).update(enable_multi_mode=False, mode_availability=availability)
        _log(Log, mode, ACTION_MODE, before, after, {"parent_equipment_id": mode.parent_equipment_id})

    base_ids_with_modes = set(
        Equipment.objects.filter(parent_equipment__isnull=False).values_list("parent_equipment_id", flat=True)
    )
    for base in Equipment.objects.filter(parent_equipment__isnull=True).order_by("pk"):
        wanted = base.pk in base_ids_with_modes
        if base.enable_multi_mode == wanted:
            continue
        Equipment.objects.filter(pk=base.pk).update(enable_multi_mode=wanted)
        _log(Log, base, ACTION_BASE, {"enable_multi_mode": base.enable_multi_mode}, {"enable_multi_mode": wanted})


def backward(apps, schema_editor):
    Equipment = apps.get_model("equipment", "Equipment")
    Log = apps.get_model("equipment", "EquipmentModeAuditLog")

    rows = Log.objects.filter(action__in=(ACTION_MODE, ACTION_BASE), details__migration=MIGRATION_TAG)
    for row in rows.order_by("-id"):
        before = (row.details or {}).get("before") or {}
        if row.equipment_id is None or "enable_multi_mode" not in before:
            continue
        fields = {"enable_multi_mode": bool(before["enable_multi_mode"])}
        if "mode_availability" in before:
            fields["mode_availability"] = before["mode_availability"]
        Equipment.objects.filter(pk=row.equipment_id).update(**fields)
        print(f"  [{MIGRATION_TAG}] restored {row.equipment_code}: {fields}")
    rows.delete()


class Migration(migrations.Migration):

    dependencies = [
        ("equipment", "0225_multimode_simplify"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
    ]
