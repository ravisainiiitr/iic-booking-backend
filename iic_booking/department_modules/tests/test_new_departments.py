import importlib
import uuid

import pytest
from django.apps import apps

from iic_booking.department_modules import access, seeding, services
from iic_booking.department_modules.constants import LOCAL_MODULES, ModuleKey
from iic_booking.department_modules.models import (
    DepartmentModuleAuditLog,
    DepartmentModuleSetting,
    DepartmentModulesInstallation,
)
from iic_booking.equipment.models import Booking
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType

from .conftest import make_booking, make_department, make_equipment, make_user, switch

frozen_migration = importlib.import_module("iic_booking.department_modules.migrations.0002_seed_initial_state")


def bulk_department(name):
    """A department inserted without the post_save signal (bulk import, fixtures, raw SQL)."""
    tag = uuid.uuid4().hex[:6].upper()
    Department.objects.bulk_create([Department(name=name, code=f"NB{tag[:4]}", department_type="internal")])
    return Department.objects.get(name=name)


def matrix_cell(department, key):
    return next(d for d in services.matrix()["departments"] if d["id"] == department.id)["cells"][key]


@pytest.mark.django_db
def test_department_created_after_installation_starts_off(installed):
    dept = make_department("Brand New Dept")

    rows = {r.module_key: r for r in DepartmentModuleSetting.objects.filter(department=dept)}
    assert set(rows) == set(LOCAL_MODULES)
    for row in rows.values():
        assert not row.enabled and not row.test_users_only
        assert row.source == "new" and row.disabled_at is not None
    assert DepartmentModuleAuditLog.objects.filter(department=dept, action="module.new_department_off").count() == 3

    student = make_user(department=dept)
    tester = make_user(department=dept, is_test_account=True)
    equipment = make_equipment(dept)
    for key in LOCAL_MODULES:
        assert not access.equipment_allows(equipment, key, student)
        assert not access.equipment_allows(equipment, key, tester)
        assert not access.user_department_allows(student, key)
        cell = matrix_cell(dept, key)
        assert cell["enabled"] is False and cell["configured"] is True and cell["source"] == "new"
    assert matrix_cell(dept, ModuleKey.PROCUREMENT)["enabled"] is False


@pytest.mark.django_db
def test_department_without_row_created_after_installation_is_off(installed):
    dept = bulk_department("Bulk Imported Dept")
    assert not DepartmentModuleSetting.objects.filter(department=dept).exists()
    student = make_user(department=dept)
    equipment = make_equipment(dept)
    booking = make_booking(student, equipment)

    for key in LOCAL_MODULES:
        assert access.cell(dept.id, key) == access.NEW_DEPARTMENT_OFF
        assert dept.id in access.blocked_department_ids(key)
        assert not access.equipment_allows(equipment, key, student, started_at=booking.created_at)
        assert not Booking.objects.filter(access.allowed_bookings_q(key), pk=booking.pk).exists()
        cell = matrix_cell(dept, key)
        assert cell["enabled"] is False and cell["configured"] is False and cell["source"] == "new"
        assert cell["note"] == services.NEW_DEPARTMENT_NOTE


@pytest.mark.django_db
def test_main_admin_turns_a_new_department_on(installed):
    admin = make_user(user_type=UserType.ADMIN)
    dept = bulk_department("Pilot Dept")
    student = make_user(department=dept)
    equipment = make_equipment(dept)

    switch(admin, dept, ModuleKey.REMOTE_ANALYSIS, enabled=True)

    assert access.equipment_allows(equipment, ModuleKey.REMOTE_ANALYSIS, student)
    assert not access.equipment_allows(equipment, ModuleKey.DSA, student)
    entry = DepartmentModuleAuditLog.objects.get(department=dept, module_key=ModuleKey.REMOTE_ANALYSIS)
    assert entry.action == "module.enabled"
    assert entry.old_value == {"enabled": False, "test_users_only": False, "configured": False}


@pytest.mark.django_db
def test_departments_from_before_installation_are_unchanged(world, installed):
    assert access.new_department_ids() == set()
    for key in LOCAL_MODULES:
        assert access.cell(world.dept.id, key) == access.UNCONFIGURED
        assert access.equipment_allows(world.equipment, key, world.student)
    assert not DepartmentModuleSetting.objects.filter(department=world.dept).exists()


@pytest.mark.django_db
def test_without_installation_new_departments_are_not_restricted():
    dept = make_department("Not Installed Dept")
    assert not DepartmentModuleSetting.objects.exists()
    assert access.installed_at() is None
    assert access.cell(dept.id, ModuleKey.DSA) == access.UNCONFIGURED


@pytest.mark.django_db
def test_frozen_migration_seeds_every_existing_department(settings):
    settings.TRAINING_MODULE_ENABLED = False
    settings.TRAINING_PILOT_EQUIPMENT_CODES = ""
    iic = make_department("Institute Instrumentation Centre", code=f"I{uuid.uuid4().hex[:5]}")
    ra_dept = make_department("RA Users Dept")
    make_equipment(ra_dept, enable_remote_analysis=True)
    idle = make_department("Idle Dept")
    make_equipment(idle)
    general = Department.objects.filter(code__iexact="GENERAL").first()

    frozen_migration.seed_initial_state(apps, None)

    installation = DepartmentModulesInstallation.objects.get(pk=1)
    rows = {(r.department_id, r.module_key): r for r in DepartmentModuleSetting.objects.all()}
    for dept_id in Department.objects.values_list("pk", flat=True):
        assert all((dept_id, key) in rows for key in LOCAL_MODULES)
    assert rows[(iic.id, "dsa")].enabled and rows[(iic.id, "remote_analysis")].enabled
    assert rows[(ra_dept.id, "remote_analysis")].enabled and not rows[(ra_dept.id, "dsa")].enabled
    for dept in filter(None, (idle, general)):
        assert not rows[(dept.id, "dsa")].enabled and not rows[(dept.id, "remote_analysis")].enabled

    # The frozen copy agrees with the live rules on the same data.
    facts, live = seeding.plan(apps.get_model)
    for f in facts:
        for key, s in live[f.id].items():
            assert (rows[(f.id, key)].enabled, rows[(f.id, key)].test_users_only) == (s.enabled, s.test_users_only)

    # Idempotent, and departments created afterwards start off.
    frozen_migration.seed_initial_state(apps, None)
    assert DepartmentModuleSetting.objects.count() == len(rows)
    assert DepartmentModulesInstallation.objects.get(pk=1).installed_at == installation.installed_at
    later = make_department("Later Dept")
    assert all(not access.cell(later.id, key).enabled for key in LOCAL_MODULES)


@pytest.mark.django_db
def test_live_seed_leaves_new_departments_off(installed):
    dept = bulk_department("Usage After Install Dept")
    make_equipment(dept, enable_remote_analysis=True)

    created = seeding.seed(apps.get_model)

    assert all(dept_id != dept.id for dept_id, _, _ in created)
    assert not DepartmentModuleSetting.objects.filter(department=dept).exists()
    assert not access.cell(dept.id, ModuleKey.REMOTE_ANALYSIS).enabled
