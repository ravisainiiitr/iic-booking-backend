import uuid

import pytest
from django.apps import apps

from iic_booking.department_modules import seeding
from iic_booking.department_modules.models import DepartmentModuleAuditLog, DepartmentModuleSetting
from iic_booking.department_modules.seeding import DepartmentFacts, TrainingGlobals, compute_initial_matrix

from .conftest import make_department, make_equipment, make_user

IIC = DepartmentFacts(id=33, code="IIC", name="Institute Instrumentation Centre",
                      dsa={"sync_profiles": 32, "agents": 2}, remote_analysis={"ra_equipment": 1},
                      training_equipment=1, test_accounts=5)
TL = DepartmentFacts(id=47, code="TL", name="Rethink ! The Tinkering Lab", test_accounts=1)
CHEM = DepartmentFacts(id=40, code="CY", name="Chemistry")
PROD_TRAINING = TrainingGlobals(module_enabled=True, audience_everyone=False, all_equipment_scope=False)


def state(seed):
    return "off" if not seed.enabled else "test-only" if seed.test_users_only else "on"


def test_production_like_matrix():
    m = compute_initial_matrix([IIC, TL, CHEM], PROD_TRAINING)
    assert {k: state(v) for k, v in m[33].items()} == {"dsa": "on", "remote_analysis": "on", "training": "on"}
    assert {k: state(v) for k, v in m[47].items()} == {"dsa": "off", "remote_analysis": "off", "training": "test-only"}
    assert {k: state(v) for k, v in m[40].items()} == {"dsa": "off", "remote_analysis": "off", "training": "off"}


def test_iic_is_on_even_without_usage():
    bare_iic = DepartmentFacts(id=1, code="iic", name="Something")
    by_name = DepartmentFacts(id=2, code="", name="Institute Instrumentation Centre")
    m = compute_initial_matrix([bare_iic, by_name], TrainingGlobals())
    for dept in (1, 2):
        assert m[dept]["dsa"].enabled and m[dept]["remote_analysis"].enabled
        assert not m[dept]["training"].enabled


@pytest.mark.parametrize(
    "signals",
    [{"sync_profiles": 1}, {"active_assignments": 1}, {"agents": 1}, {"workspaces": 3}, {"legacy_agents": 1},
     {"equipment_pcs": 1}],
)
def test_dsa_on_for_any_real_usage(signals):
    m = compute_initial_matrix([DepartmentFacts(id=5, code="X", dsa=signals)], TrainingGlobals())
    assert m[5]["dsa"].enabled and not m[5]["dsa"].test_users_only


@pytest.mark.parametrize(
    "signals", [{"ra_equipment": 1}, {"workstations": 1}, {"reservations": 2}, {"analysis_workspaces": 1}]
)
def test_raa_on_for_any_real_usage(signals):
    m = compute_initial_matrix([DepartmentFacts(id=5, code="X", remote_analysis=signals)], TrainingGlobals())
    assert m[5]["remote_analysis"].enabled


def test_training_rules():
    owner = DepartmentFacts(id=1, code="A", training_equipment=2)
    records = DepartmentFacts(id=2, code="B", training_records=4)
    testers = DepartmentFacts(id=3, code="C", test_accounts=2)
    nobody = DepartmentFacts(id=4, code="D")
    facts = [owner, records, testers, nobody]

    m = compute_initial_matrix(facts, TrainingGlobals(module_enabled=True))
    assert [state(m[i]["training"]) for i in (1, 2, 3, 4)] == ["on", "on", "test-only", "off"]

    everyone = compute_initial_matrix(facts, TrainingGlobals(module_enabled=True, audience_everyone=True))
    assert [state(everyone[i]["training"]) for i in (1, 2, 3, 4)] == ["on", "on", "on", "on"]

    env_all = compute_initial_matrix(facts, TrainingGlobals(module_enabled=True, all_equipment_scope=True))
    assert all(env_all[i]["training"].enabled for i in (1, 2, 3, 4))

    module_off = compute_initial_matrix(facts, TrainingGlobals(module_enabled=False))
    assert [state(module_off[i]["training"]) for i in (1, 2, 3, 4)] == ["on", "on", "off", "off"]


@pytest.mark.django_db
def test_collect_and_seed_from_data_is_idempotent(settings):
    from iic_booking.remote_analysis.models import AnalysisWorkstation
    from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent
    from iic_booking.training.models import TrainingEquipmentSetting, TrainingModuleSettings

    settings.TRAINING_MODULE_ENABLED = False
    settings.TRAINING_PILOT_EQUIPMENT_CODES = ""
    iic = make_department("Institute Instrumentation Centre", code=f"I{uuid.uuid4().hex[:5]}")
    dsa_dept = make_department("DSA Dept")
    ra_dept = make_department("RA Dept")
    tr_dept = make_department("Training Dept")
    tester_dept = make_department("Tester Dept")
    idle = make_department("Idle Dept")
    make_equipment(idle)  # dsa_enabled defaults to True and must not count as DSA usage
    DepartmentSyncAgent.objects.create(
        agent_name="Agent", department=dsa_dept, machine_guid=uuid.uuid4(), status=AgentLifecycleStatus.ENROLLED,
        is_active=True,
    )
    make_equipment(ra_dept, enable_remote_analysis=True)
    AnalysisWorkstation.objects.create(agent_id=f"ws-{uuid.uuid4().hex[:8]}", department=ra_dept)
    TrainingEquipmentSetting.objects.create(equipment=make_equipment(tr_dept), enabled=True)
    TrainingModuleSettings.objects.update_or_create(pk=1, defaults={"module_enabled": True, "audience": "TEST_ACCOUNTS"})
    make_user(department=tester_dept, is_test_account=True)

    created = seeding.seed(apps.get_model)
    rows = {(r.department_id, r.module_key): r for r in DepartmentModuleSetting.objects.all()}
    assert len(created) == len(rows)

    def st(dept, key):
        r = rows[(dept.id, key)]
        return "off" if not r.enabled else "test-only" if r.test_users_only else "on"

    assert st(dsa_dept, "dsa") == "on" and st(dsa_dept, "remote_analysis") == "off"
    assert st(ra_dept, "remote_analysis") == "on" and st(ra_dept, "dsa") == "off"
    assert st(tr_dept, "training") == "on"
    assert st(tester_dept, "training") == "test-only"
    assert [st(idle, k) for k in ("dsa", "remote_analysis", "training")] == ["off", "off", "off"]
    # IIC is matched by name here (code is random) and is always ON for DSA and RAA.
    assert st(iic, "dsa") == "on" and st(iic, "remote_analysis") == "on"
    off_row = rows[(idle.id, "dsa")]
    assert off_row.disabled_at is not None and off_row.source == "seed" and off_row.seed_note
    assert rows[(tester_dept.id, "training")].test_only_since is not None
    assert DepartmentModuleAuditLog.objects.filter(action="module.seeded").count() == len(created)

    # Idempotent: a second run creates nothing and never overrides an admin choice.
    DepartmentModuleSetting.objects.filter(department=idle, module_key="dsa").update(enabled=True)
    assert seeding.seed(apps.get_model) == []
    assert DepartmentModuleSetting.objects.get(department=idle, module_key="dsa").enabled is True


@pytest.mark.django_db
def test_seed_covers_every_department_and_records_installation():
    from iic_booking.department_modules.models import DepartmentModulesInstallation
    from iic_booking.users.models.department import Department

    fresh = make_department("Fresh Dept")
    assert not DepartmentModulesInstallation.objects.exists()
    seeding.seed(apps.get_model)
    assert DepartmentModulesInstallation.objects.filter(pk=1).exists()
    assert DepartmentModuleSetting.objects.filter(department=fresh).count() == 3
    assert DepartmentModuleSetting.objects.count() == 3 * Department.objects.count()


@pytest.mark.django_db
def test_plan_command_prints_matrix(capsys):
    from django.core.management import call_command

    make_department("Institute Instrumentation Centre", code="IIC")
    call_command("department_modules", "--plan")
    out = capsys.readouterr().out
    assert "DEPARTMENT" in out and "Why ON" in out
    call_command("department_modules", "--seed")
    assert DepartmentModuleSetting.objects.exists()
