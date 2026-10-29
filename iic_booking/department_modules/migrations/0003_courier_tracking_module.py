"""Add the Courier Tracking module switch and seed it: on for IIC, off for every other department.

Self-contained (historical models only). Creates only missing rows; never changes an existing one. Departments created
later start off through ``services.start_new_department_off``. Reversing removes only the rows this migration seeded
(rows changed by an administrator since are kept, and the field choices are restored).
"""

from django.db import migrations, models
from django.utils import timezone

COURIER_TRACKING = "courier_tracking"
IIC_CODES = {"IIC"}
IIC_NAMES = {"institute instrumentation centre", "institute instrumentation center"}
SEED_NOTE_ON = "IIC (central facility): courier tracking on by default"
SEED_NOTE_OFF = "courier tracking starts off; enable it explicitly"


def _is_iic(dept):
    return (dept.code or "").strip().upper() in IIC_CODES or (dept.name or "").strip().lower() in IIC_NAMES


def seed_courier_tracking(apps, schema_editor):
    Department = apps.get_model("users", "Department")
    Setting = apps.get_model("department_modules", "DepartmentModuleSetting")
    AuditLog = apps.get_model("department_modules", "DepartmentModuleAuditLog")
    now = timezone.now()
    existing = set(Setting.objects.filter(module_key=COURIER_TRACKING).values_list("department_id", flat=True))
    for dept in Department.objects.order_by("name"):
        if dept.pk in existing:
            continue
        enabled = _is_iic(dept)
        note = SEED_NOTE_ON if enabled else SEED_NOTE_OFF
        Setting.objects.create(
            department_id=dept.pk,
            module_key=COURIER_TRACKING,
            enabled=enabled,
            test_users_only=False,
            disabled_at=None if enabled else now,
            source="seed",
            seed_note=note,
        )
        AuditLog.objects.create(
            department_id=dept.pk,
            department_label=(dept.code or dept.name or "")[:255],
            module_key=COURIER_TRACKING,
            actor=None,
            action="module.seeded",
            old_value={},
            new_value={"enabled": enabled, "test_users_only": False, "configured": True},
            reason=f"Initial state: {note}",
            created_at=now,
        )


def unseed_courier_tracking(apps, schema_editor):
    Setting = apps.get_model("department_modules", "DepartmentModuleSetting")
    # Rows are protected from deletion in application code; the migration removes only untouched seed rows.
    Setting.objects.filter(module_key=COURIER_TRACKING, source="seed").delete()


class Migration(migrations.Migration):

    dependencies = [
        ("department_modules", "0002_seed_initial_state"),
        ("users", "0140_sric_signin_notice"),
    ]

    operations = [
        migrations.AlterField(
            model_name="departmentmoduleauditlog",
            name="module_key",
            field=models.CharField(
                choices=[
                    ("dsa", "Department Sync Agent"),
                    ("remote_analysis", "Remote Analysis"),
                    ("training", "Training & Certification"),
                    ("procurement", "Procurement & Assets"),
                    ("courier_tracking", "Courier Tracking"),
                ],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="departmentmodulesetting",
            name="module_key",
            field=models.CharField(
                choices=[
                    ("dsa", "Department Sync Agent"),
                    ("remote_analysis", "Remote Analysis"),
                    ("training", "Training & Certification"),
                    ("courier_tracking", "Courier Tracking"),
                ],
                max_length=32,
            ),
        ),
        migrations.RunPython(seed_courier_tracking, unseed_courier_tracking),
    ]
