"""Seed the per-department switches from what each department uses today and record the installation.

Every department that exists when this runs gets an explicit row per module (DSA, Remote Analysis, Training);
departments created later start off (see ``DepartmentModulesInstallation``). Only creates missing rows.

The rules are a frozen copy of ``iic_booking.department_modules.seeding`` as of this release and use historical
models only, so later changes to the live module never change what this migration does. Keep it self-contained.

* DSA ON    - IIC, or the department has sync profiles, active agent assignments, department sync agents, legacy
              sync agents, booking workspaces or provisioned Equipment PCs (``Equipment.dsa_enabled`` is ignored).
* RAA ON    - IIC, or the department has Remote-Analysis-enabled equipment, analysis workstations, or analysis
              reservations / workspaces on its equipment.
* Training  - all departments while the env switch puts every equipment in scope; otherwise departments owning
              Training-enabled equipment (DB or env codes) or with training records; all while the module is on with
              audience "everyone"; with audience "test accounts" a department with test accounts is ON test-only.
"""

from collections import defaultdict

from django.conf import settings
from django.db import migrations
from django.db.models import Count, Q
from django.utils import timezone

DSA = "dsa"
REMOTE_ANALYSIS = "remote_analysis"
TRAINING = "training"
IIC_CODES = {"IIC"}
IIC_NAMES = {"institute instrumentation centre", "institute instrumentation center"}
TEST_EMAILS = ("test.student@iic-booking.test", "test.faculty@iic-booking.test")


def _model(apps, app_label, name):
    try:
        return apps.get_model(app_label, name)
    except LookupError:
        return None


def _count_into(target, key, qs, field_name):
    for dept_id, n in qs.values_list(field_name).annotate(n=Count("pk")).values_list(field_name, "n"):
        if dept_id:
            target[dept_id][key] = target[dept_id].get(key, 0) + n


def _csv_upper(raw):
    return {p.strip().upper() for p in str(raw or "").split(",") if p.strip()}


def _signals(counts):
    return ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in sorted(counts.items()) if n)


def _is_iic(dept):
    return (dept.code or "").strip().upper() in IIC_CODES or (dept.name or "").strip().lower() in IIC_NAMES


def _usage_seed(dept, counts, label):
    used = _signals(counts)
    if _is_iic(dept):
        return True, False, f"IIC (central facility){'; ' + used if used else ''}"
    if used:
        return True, False, f"in use: {used}"
    return False, False, f"no {label} in use"


def _training_seed(training_eq, training_records, test_accounts, module_enabled, audience_everyone, all_scope):
    if all_scope:
        return True, False, "Training env switch covers every equipment"
    if training_eq:
        return True, False, f"owns {training_eq} Training-enabled equipment"
    if training_records:
        return True, False, f"{training_records} training records"
    if module_enabled and audience_everyone:
        return True, False, "Training is open to everyone"
    if module_enabled and test_accounts:
        return True, True, f"{test_accounts} test accounts can see Training today"
    return False, False, "no Training equipment, records or audience"


def seed_initial_state(apps, schema_editor):
    Department = apps.get_model("users", "Department")
    Equipment = apps.get_model("equipment", "Equipment")
    User = apps.get_model("users", "User")
    Setting = apps.get_model("department_modules", "DepartmentModuleSetting")
    AuditLog = apps.get_model("department_modules", "DepartmentModuleAuditLog")
    Installation = apps.get_model("department_modules", "DepartmentModulesInstallation")

    now = timezone.now()
    Installation.objects.get_or_create(pk=1, defaults={"installed_at": now})

    dsa = defaultdict(dict)
    ra = defaultdict(dict)
    SyncProfile = _model(apps, "sync", "EquipmentSyncProfile")
    if SyncProfile is not None:
        _count_into(dsa, "sync_profiles", SyncProfile.objects.all(), "equipment__internal_department_id")
    Assignment = _model(apps, "sync", "AgentAssignment")
    if Assignment is not None:
        _count_into(
            dsa, "active_assignments", Assignment.objects.filter(is_active=True), "sync_profile__equipment__internal_department_id"
        )
    DeptAgent = _model(apps, "sync", "DepartmentSyncAgent")
    if DeptAgent is not None:
        _count_into(dsa, "agents", DeptAgent.objects.all(), "department_id")
    Workspace = _model(apps, "sync", "BookingWorkspace")
    if Workspace is not None:
        _count_into(dsa, "workspaces", Workspace.objects.all(), "equipment__internal_department_id")
    LegacyAgent = _model(apps, "users", "SyncAgent")
    if LegacyAgent is not None:
        _count_into(dsa, "legacy_agents", LegacyAgent.objects.all(), "department_id")
    DeviceAssignment = _model(apps, "device_provisioning", "DeviceAssignment")
    if DeviceAssignment is not None:
        _count_into(
            dsa, "equipment_pcs", DeviceAssignment.objects.filter(equipment__isnull=False), "equipment__internal_department_id"
        )

    _count_into(ra, "ra_equipment", Equipment.objects.filter(enable_remote_analysis=True), "internal_department_id")
    Workstation = _model(apps, "remote_analysis", "AnalysisWorkstation")
    if Workstation is not None:
        _count_into(ra, "workstations", Workstation.objects.all(), "department_id")
    Reservation = _model(apps, "remote_analysis", "AnalysisReservation")
    if Reservation is not None:
        _count_into(
            ra, "reservations", Reservation.objects.filter(booking__isnull=False), "booking__equipment__internal_department_id"
        )
    AnalysisWorkspace = _model(apps, "remote_analysis", "AnalysisWorkspace")
    if AnalysisWorkspace is not None:
        _count_into(
            ra,
            "analysis_workspaces",
            AnalysisWorkspace.objects.filter(booking__isnull=False),
            "booking__equipment__internal_department_id",
        )

    training_eq = defaultdict(int)
    training_records = defaultdict(int)
    env_on = bool(getattr(settings, "TRAINING_MODULE_ENABLED", False))
    env_codes = _csv_upper(getattr(settings, "TRAINING_PILOT_EQUIPMENT_CODES", ""))
    db_enabled_ids = set()
    EquipmentSetting = _model(apps, "training", "TrainingEquipmentSetting")
    if EquipmentSetting is not None:
        db_enabled_ids = set(EquipmentSetting.objects.filter(enabled=True).values_list("equipment_id", flat=True))
    scope_ids = set(db_enabled_ids)
    if env_codes:
        scope_ids |= set(Equipment.objects.filter(code__in=env_codes).values_list("pk", flat=True))
    for dept_id, n in (
        Equipment.objects.filter(pk__in=scope_ids)
        .values_list("internal_department_id")
        .annotate(n=Count("pk"))
        .values_list("internal_department_id", "n")
    ):
        if dept_id:
            training_eq[dept_id] += n
    ModuleSettings = _model(apps, "training", "TrainingModuleSettings")
    row = ModuleSettings.objects.filter(pk=1).first() if ModuleSettings is not None else None
    module_enabled = env_on or bool(row and row.module_enabled)
    audience_everyone = bool(row and row.audience == "EVERYONE")
    all_scope = env_on and not env_codes and not db_enabled_ids
    for app_label, name, user_field, eq_field in (
        ("training", "DemoRequest", "requester__department_id", "equipment__internal_department_id"),
        ("training", "TrainingNomination", "student__department_id", "call__equipment__internal_department_id"),
        ("training", "Registration", "user__department_id", None),
    ):
        Model = _model(apps, app_label, name)
        if Model is None:
            continue
        for fld in filter(None, (user_field, eq_field)):
            for dept_id, n in Model.objects.values_list(fld).annotate(n=Count("pk")).values_list(fld, "n"):
                if dept_id:
                    training_records[dept_id] += n

    test_q = Q(is_test_account=True)
    for email in TEST_EMAILS:
        test_q |= Q(email__iexact=email)
    test_accounts = {
        k: n
        for k, n in User.objects.filter(test_q).values_list("department_id").annotate(n=Count("pk")).values_list("department_id", "n")
        if k
    }

    existing = set(Setting.objects.values_list("department_id", "module_key"))
    for dept in Department.objects.order_by("name"):
        seeds = {
            DSA: _usage_seed(dept, dsa.get(dept.pk, {}), "DSA profiles, agents or workspaces"),
            REMOTE_ANALYSIS: _usage_seed(dept, ra.get(dept.pk, {}), "Remote Analysis equipment or sessions"),
            TRAINING: _training_seed(
                training_eq.get(dept.pk, 0),
                training_records.get(dept.pk, 0),
                test_accounts.get(dept.pk, 0),
                module_enabled,
                audience_everyone,
                all_scope,
            ),
        }
        for key, (enabled, test_only, note) in seeds.items():
            if (dept.pk, key) in existing:
                continue
            Setting.objects.create(
                department_id=dept.pk,
                module_key=key,
                enabled=enabled,
                test_users_only=test_only,
                disabled_at=None if enabled else now,
                test_only_since=now if test_only else None,
                source="seed",
                seed_note=note[:255],
            )
            AuditLog.objects.create(
                department_id=dept.pk,
                department_label=(dept.code or dept.name or "")[:255],
                module_key=key,
                actor=None,
                action="module.seeded",
                old_value={},
                new_value={"enabled": enabled, "test_users_only": test_only, "configured": True},
                reason=f"Initial state from existing data: {note}"[:2000],
                created_at=now,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("department_modules", "0001_initial"),
        ("users", "0130_procurement_user_types"),
        ("equipment", "0224_charge_copy_batch"),
        ("sync", "0018_equipment_pc_ip_reservation"),
        ("remote_analysis", "0028_browse_pc_folders_command"),
        ("training", "0005_demo_charge_waived"),
        ("device_provisioning", "0003_equipment_pc_audit_actions_r24"),
    ]

    operations = [
        migrations.RunPython(seed_initial_state, migrations.RunPython.noop),
    ]
