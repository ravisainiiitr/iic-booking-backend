"""Starting state of the department switches, derived from what each department actually uses today.

``compute_initial_matrix`` is a pure function (unit-tested on fixtures). ``collect_facts`` gathers its inputs through
a ``get_model(app_label, model_name)`` callable. The data migration ``0002_seed_initial_state`` carries a frozen copy
of these rules (so later changes here never change what the migration did); this module serves the
``department_modules`` command. ``seed`` only ever creates missing rows, so it is idempotent and never overrides a
choice the Main Administrator already made.

Rules (nothing that works today may stop working):
* DSA ON    - IIC, or the department has sync profiles, active agent assignments, department sync agents, legacy
              sync agents, booking workspaces or provisioned Equipment PCs. ``Equipment.dsa_enabled`` alone is not a
              signal: it defaults to True for every equipment.
* RAA ON    - IIC, or the department has Remote-Analysis-enabled equipment, analysis workstations, or analysis
              reservations / workspaces on its equipment.
* Training  - all departments while the env switch puts every equipment in scope, or while the module is on with
              audience "everyone"; otherwise departments owning Training-enabled equipment (DB or env codes) or with
              training records; with audience "test accounts" a department whose test accounts can see Training
              today is ON with test-users-only (so exactly the same people keep seeing it).
* Procurement keeps its own per-department configuration and is not seeded.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone

from .constants import ModuleKey

IIC_CODES = {"IIC"}
IIC_NAMES = {"institute instrumentation centre", "institute instrumentation center"}
TEST_EMAILS = ("test.student@iic-booking.test", "test.faculty@iic-booking.test")


@dataclass
class DepartmentFacts:
    id: int
    code: str = ""
    name: str = ""
    dsa: dict[str, int] = field(default_factory=dict)
    remote_analysis: dict[str, int] = field(default_factory=dict)
    training_equipment: int = 0
    training_records: int = 0
    test_accounts: int = 0


@dataclass
class TrainingGlobals:
    module_enabled: bool = False
    audience_everyone: bool = False
    all_equipment_scope: bool = False


@dataclass(frozen=True)
class Seed:
    enabled: bool
    test_users_only: bool
    note: str


def is_iic(facts: DepartmentFacts) -> bool:
    return (facts.code or "").strip().upper() in IIC_CODES or (facts.name or "").strip().lower() in IIC_NAMES


def _signals(counts: dict[str, int]) -> str:
    return ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in sorted(counts.items()) if n)


def _usage_seed(facts: DepartmentFacts, counts: dict[str, int], label: str) -> Seed:
    used = _signals(counts)
    if is_iic(facts):
        return Seed(True, False, f"IIC (central facility){'; ' + used if used else ''}")
    if used:
        return Seed(True, False, f"in use: {used}")
    return Seed(False, False, f"no {label} in use")


def _training_seed(facts: DepartmentFacts, training: TrainingGlobals) -> Seed:
    if training.all_equipment_scope:
        return Seed(True, False, "Training env switch covers every equipment")
    if facts.training_equipment:
        return Seed(True, False, f"owns {facts.training_equipment} Training-enabled equipment")
    if facts.training_records:
        return Seed(True, False, f"{facts.training_records} training records")
    if training.module_enabled and training.audience_everyone:
        return Seed(True, False, "Training is open to everyone")
    if training.module_enabled and facts.test_accounts:
        return Seed(True, True, f"{facts.test_accounts} test accounts can see Training today")
    return Seed(False, False, "no Training equipment, records or audience")


def compute_initial_matrix(facts: list[DepartmentFacts], training: TrainingGlobals) -> dict[int, dict[str, Seed]]:
    return {
        f.id: {
            ModuleKey.DSA.value: _usage_seed(f, f.dsa, "DSA profiles, agents or workspaces"),
            ModuleKey.REMOTE_ANALYSIS.value: _usage_seed(f, f.remote_analysis, "Remote Analysis equipment or sessions"),
            ModuleKey.TRAINING.value: _training_seed(f, training),
        }
        for f in facts
    }


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
GetModel = Callable[[str, str], object]


def _model(get_model: GetModel, app: str, name: str):
    try:
        return get_model(app, name)
    except LookupError:
        return None


def _count_into(target: dict[int, dict[str, int]], key: str, qs, field_name: str) -> None:
    for dept_id, n in qs.values_list(field_name).annotate(n=Count("pk")).values_list(field_name, "n"):
        if dept_id:
            target[dept_id][key] = target[dept_id].get(key, 0) + n


def _csv_upper(raw) -> set[str]:
    return {p.strip().upper() for p in str(raw or "").split(",") if p.strip()}


def collect_facts(get_model: GetModel) -> tuple[list[DepartmentFacts], TrainingGlobals]:
    Department = get_model("users", "Department")
    Equipment = get_model("equipment", "Equipment")
    User = get_model("users", "User")

    dsa: dict[int, dict[str, int]] = defaultdict(dict)
    ra: dict[int, dict[str, int]] = defaultdict(dict)

    SyncProfile = _model(get_model, "sync", "EquipmentSyncProfile")
    if SyncProfile is not None:
        _count_into(dsa, "sync_profiles", SyncProfile.objects.all(), "equipment__internal_department_id")
    Assignment = _model(get_model, "sync", "AgentAssignment")
    if Assignment is not None:
        _count_into(
            dsa, "active_assignments", Assignment.objects.filter(is_active=True), "sync_profile__equipment__internal_department_id"
        )
    DeptAgent = _model(get_model, "sync", "DepartmentSyncAgent")
    if DeptAgent is not None:
        _count_into(dsa, "agents", DeptAgent.objects.all(), "department_id")
    Workspace = _model(get_model, "sync", "BookingWorkspace")
    if Workspace is not None:
        _count_into(dsa, "workspaces", Workspace.objects.all(), "equipment__internal_department_id")
    LegacyAgent = _model(get_model, "users", "SyncAgent")
    if LegacyAgent is not None:
        _count_into(dsa, "legacy_agents", LegacyAgent.objects.all(), "department_id")
    DeviceAssignment = _model(get_model, "device_provisioning", "DeviceAssignment")
    if DeviceAssignment is not None:
        _count_into(
            dsa,
            "equipment_pcs",
            DeviceAssignment.objects.filter(equipment__isnull=False),
            "equipment__internal_department_id",
        )

    _count_into(ra, "ra_equipment", Equipment.objects.filter(enable_remote_analysis=True), "internal_department_id")
    Workstation = _model(get_model, "remote_analysis", "AnalysisWorkstation")
    if Workstation is not None:
        _count_into(ra, "workstations", Workstation.objects.all(), "department_id")
    Reservation = _model(get_model, "remote_analysis", "AnalysisReservation")
    if Reservation is not None:
        _count_into(
            ra, "reservations", Reservation.objects.filter(booking__isnull=False), "booking__equipment__internal_department_id"
        )
    AnalysisWorkspace = _model(get_model, "remote_analysis", "AnalysisWorkspace")
    if AnalysisWorkspace is not None:
        _count_into(
            ra,
            "analysis_workspaces",
            AnalysisWorkspace.objects.filter(booking__isnull=False),
            "booking__equipment__internal_department_id",
        )

    training_eq: dict[int, int] = defaultdict(int)
    training_records: dict[int, int] = defaultdict(int)
    env_on = bool(getattr(settings, "TRAINING_MODULE_ENABLED", False))
    env_codes = _csv_upper(getattr(settings, "TRAINING_PILOT_EQUIPMENT_CODES", ""))
    db_enabled_ids: set[int] = set()
    training = TrainingGlobals()
    EquipmentSetting = _model(get_model, "training", "TrainingEquipmentSetting")
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
    ModuleSettings = _model(get_model, "training", "TrainingModuleSettings")
    row = ModuleSettings.objects.filter(pk=1).first() if ModuleSettings is not None else None
    training.module_enabled = env_on or bool(row and row.module_enabled)
    training.audience_everyone = bool(row and row.audience == "EVERYONE")
    training.all_equipment_scope = env_on and not env_codes and not db_enabled_ids
    for app, name, user_field, eq_field in (
        ("training", "DemoRequest", "requester__department_id", "equipment__internal_department_id"),
        ("training", "TrainingNomination", "student__department_id", "call__equipment__internal_department_id"),
        ("training", "Registration", "user__department_id", None),
    ):
        Model = _model(get_model, app, name)
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

    facts = [
        DepartmentFacts(
            id=d.pk,
            code=d.code or "",
            name=d.name or "",
            dsa=dict(dsa.get(d.pk, {})),
            remote_analysis=dict(ra.get(d.pk, {})),
            training_equipment=training_eq.get(d.pk, 0),
            training_records=training_records.get(d.pk, 0),
            test_accounts=test_accounts.get(d.pk, 0),
        )
        for d in Department.objects.order_by("name")
    ]
    return facts, training


def plan(get_model: GetModel) -> tuple[list[DepartmentFacts], dict[int, dict[str, Seed]]]:
    facts, training = collect_facts(get_model)
    return facts, compute_initial_matrix(facts, training)


def seed(get_model: GetModel, *, now=None) -> list[tuple[int, str, Seed]]:
    """Create the missing rows of the starting matrix (never changes existing rows). Returns what was created.

    Used by ``department_modules --seed``; the data migration runs a frozen copy of this logic. Departments created
    after the installation are not seeded from usage: they start off until the Main Administrator turns them on.
    Records the installation if it is missing.
    """
    Setting = get_model("department_modules", "DepartmentModuleSetting")
    AuditLog = get_model("department_modules", "DepartmentModuleAuditLog")
    Installation = get_model("department_modules", "DepartmentModulesInstallation")
    Department = get_model("users", "Department")
    now = now or timezone.now()
    installation, _ = Installation.objects.get_or_create(pk=1, defaults={"installed_at": now})
    facts, matrix = plan(get_model)
    existing = set(Setting.objects.values_list("department_id", "module_key"))
    departments = {d.pk: d for d in Department.objects.all()}
    created: list[tuple[int, str, Seed]] = []
    for f in facts:
        dept = departments.get(f.id)
        if dept is not None and dept.created_at and dept.created_at >= installation.installed_at:
            continue
        for key, s in matrix[f.id].items():
            if (f.id, key) in existing:
                continue
            Setting.objects.create(
                department_id=f.id,
                module_key=key,
                enabled=s.enabled,
                test_users_only=s.test_users_only,
                disabled_at=None if s.enabled else now,
                test_only_since=now if s.test_users_only else None,
                source="seed",
                seed_note=s.note[:255],
            )
            AuditLog.objects.create(
                department_id=f.id,
                department_label=((dept.code or dept.name) if dept else "")[:255],
                module_key=key,
                actor=None,
                action="module.seeded",
                old_value={},
                new_value={"enabled": s.enabled, "test_users_only": s.test_users_only, "configured": True},
                reason=f"Initial state from existing data: {s.note}"[:2000],
                created_at=now,
            )
            created.append((f.id, key, s))
    return created
