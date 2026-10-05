import pytest

from iic_booking.department_modules import access, services
from iic_booking.department_modules.constants import ModuleKey
from iic_booking.department_modules.errors import DepartmentModuleError
from iic_booking.department_modules.models import DepartmentModuleAuditLog, DepartmentModuleSetting
from iic_booking.procurement_management.models import ImmutableRecordError, ProcurementManagementConfiguration
from iic_booking.users.models.user_type import UserType

from .conftest import ADMIN_API, client_for, make_user, switch

pytestmark = pytest.mark.django_db


def test_unconfigured_department_behaves_as_before(world):
    cell = access.cell(world.dept.id, ModuleKey.REMOTE_ANALYSIS)
    assert cell.enabled and not cell.test_users_only and not cell.configured
    assert access.department_allows(world.dept.id, ModuleKey.DSA, world.student)


def test_only_main_admin_can_change(world):
    for user in (world.student, world.faculty, make_user(user_type=UserType.DEPT_ADMIN, department=world.dept)):
        with pytest.raises(DepartmentModuleError) as exc:
            switch(user, world.dept, ModuleKey.DSA, enabled=False)
        assert exc.value.status == 403
    assert not DepartmentModuleSetting.objects.exists()


def test_reason_is_required(world):
    with pytest.raises(DepartmentModuleError) as exc:
        services.set_module(world.admin, world.dept, ModuleKey.DSA, enabled=False, reason="  ")
    assert exc.value.code == "reason_required"
    assert not DepartmentModuleSetting.objects.exists()


def test_switch_off_records_cutoff_and_audit(world):
    cell = switch(world.admin, world.dept, ModuleKey.REMOTE_ANALYSIS, enabled=False, reason="Not ready in Chemistry")
    assert cell["enabled"] is False and cell["configured"] is True
    row = DepartmentModuleSetting.objects.get(department=world.dept, module_key=ModuleKey.REMOTE_ANALYSIS)
    assert row.disabled_at is not None and row.updated_by == world.admin
    entry = DepartmentModuleAuditLog.objects.get()
    assert entry.action == "module.disabled"
    assert entry.old_value == {"enabled": True, "test_users_only": False, "configured": False}
    assert entry.new_value == {"enabled": False, "test_users_only": False, "configured": True}
    assert entry.reason == "Not ready in Chemistry" and entry.actor == world.admin

    switch(world.admin, world.dept, ModuleKey.REMOTE_ANALYSIS, enabled=True, test_users_only=True)
    row.refresh_from_db()
    assert row.enabled and row.disabled_at is None and row.test_only_since is not None
    assert DepartmentModuleAuditLog.objects.count() == 2


def test_no_change_writes_no_audit(world):
    switch(world.admin, world.dept, ModuleKey.TRAINING, enabled=False)
    switch(world.admin, world.dept, ModuleKey.TRAINING, enabled=False)
    assert DepartmentModuleAuditLog.objects.count() == 1


def test_rows_and_audit_are_never_deleted(world):
    switch(world.admin, world.dept, ModuleKey.DSA, enabled=False)
    row = DepartmentModuleSetting.objects.get()
    with pytest.raises(ImmutableRecordError):
        row.delete()
    with pytest.raises(ImmutableRecordError):
        DepartmentModuleSetting.objects.all().delete()
    entry = DepartmentModuleAuditLog.objects.get()
    with pytest.raises(ImmutableRecordError):
        entry.delete()
    with pytest.raises(ImmutableRecordError):
        DepartmentModuleAuditLog.objects.update(reason="x")


def test_procurement_writes_its_own_configuration(world):
    """Procurement has one source of truth: ProcurementManagementConfiguration (module_enabled / pilot_mode)."""
    cell = switch(world.admin, world.dept, ModuleKey.PROCUREMENT, enabled=True, test_users_only=False)
    cfg = ProcurementManagementConfiguration.objects.get(department=world.dept)
    assert cfg.module_enabled is True and cfg.pilot_mode is False
    assert cell["enabled"] is True and cell["test_users_only"] is False
    assert not DepartmentModuleSetting.objects.filter(module_key=ModuleKey.PROCUREMENT).exists()
    assert DepartmentModuleAuditLog.objects.filter(module_key=ModuleKey.PROCUREMENT, action="module.enabled").exists()
    from iic_booking.procurement_management.models import ProcurementAuditLog

    assert ProcurementAuditLog.objects.filter(department=world.dept, action="config.module_enabled").exists()

    switch(world.admin, world.dept, ModuleKey.PROCUREMENT, test_users_only=True)
    cfg.refresh_from_db()
    assert cfg.module_enabled is True and cfg.pilot_mode is True
    matrix = services.matrix()
    row = next(d for d in matrix["departments"] if d["id"] == world.dept.id)
    assert row["cells"]["procurement"]["enabled"] is True
    assert row["cells"]["procurement"]["test_users_only"] is True


def test_matrix_api_is_main_admin_only(world):
    assert client_for().get(f"{ADMIN_API}/").status_code in (401, 403)
    assert client_for(world.student).get(f"{ADMIN_API}/").status_code == 403
    dept_admin = make_user(user_type=UserType.DEPT_ADMIN, department=world.dept)
    assert client_for(dept_admin).get(f"{ADMIN_API}/").status_code == 403
    res = client_for(world.admin).get(f"{ADMIN_API}/")
    assert res.status_code == 200
    body = res.json()
    assert [m["key"] for m in body["modules"]] == ["dsa", "remote_analysis", "training", "procurement"]
    row = next(d for d in body["departments"] if d["id"] == world.dept.id)
    assert set(row["cells"]) == {"dsa", "remote_analysis", "training", "procurement"}
    assert row["equipment_count"] == 1


def test_update_cell_api(world):
    url = f"{ADMIN_API}/{world.dept.id}/dsa/"
    assert client_for(world.student).post(url, {"enabled": False, "reason": "x" * 10}, format="json").status_code == 403
    res = client_for(world.admin).post(url, {"enabled": False}, format="json")
    assert res.status_code == 400 and res.json()["code"] == "reason_required"
    res = client_for(world.admin).post(url, {"enabled": False, "reason": "Agent PC retired"}, format="json")
    assert res.status_code == 200, res.content
    assert res.json()["cell"]["enabled"] is False
    assert client_for(world.admin).post(
        f"{ADMIN_API}/{world.dept.id}/nonsense/", {"enabled": False, "reason": "x" * 10}, format="json"
    ).status_code == 404

    hist = client_for(world.admin).get(f"{ADMIN_API}/history/", {"department": world.dept.id})
    assert hist.status_code == 200
    entries = hist.json()["results"]
    assert entries[0]["module_key"] == "dsa" and entries[0]["reason"] == "Agent PC retired"
    assert client_for(world.student).get(f"{ADMIN_API}/history/").status_code == 403


def test_user_availability_endpoint_and_me(world):
    switch(world.admin, world.dept, ModuleKey.REMOTE_ANALYSIS, enabled=False)
    switch(world.admin, world.dept, ModuleKey.DSA, test_users_only=True)
    res = client_for(world.student).get("/api/v1/department-modules/me/")
    assert res.status_code == 200
    modules = res.json()["modules"]
    assert modules["remote_analysis"]["available"] is False
    assert modules["dsa"]["available"] is False
    test_modules = client_for(world.test_student).get("/api/v1/department-modules/me/").json()["modules"]
    assert test_modules["dsa"]["available"] is True
    assert test_modules["remote_analysis"]["available"] is False
    admin = client_for(world.admin).get("/api/v1/department-modules/me/").json()
    assert admin["can_configure"] is True and admin["modules"]["remote_analysis"]["available"] is True
    # Procurement answers for itself (no department enabled here, and its pilot list applies to Main Admins too).
    assert admin["modules"]["procurement"]["available"] is False

    me = client_for(world.student).get("/api/auth/user/")
    assert me.status_code == 200
    assert me.json()["department_modules"]["modules"]["remote_analysis"]["available"] is False


def test_oic_keeps_module_through_equipment_department(world):
    from iic_booking.equipment.models import EquipmentManager

    oic = make_user(user_type=UserType.MANAGER, department=world.other_dept)
    EquipmentManager.objects.create(equipment=world.equipment, manager=oic)
    switch(world.admin, world.other_dept, ModuleKey.REMOTE_ANALYSIS, enabled=False)
    assert access.user_availability(oic)["modules"]["remote_analysis"]["available"] is True
    switch(world.admin, world.dept, ModuleKey.REMOTE_ANALYSIS, enabled=False)
    assert access.user_availability(oic)["modules"]["remote_analysis"]["available"] is False


def test_management_command_set_show_history(world, capsys):
    from django.core.management import call_command

    call_command(
        "department_modules", "--set", world.dept.code, "training", "--off", "--reason", "Not in scope", "--actor",
        world.admin.email,
    )
    assert DepartmentModuleSetting.objects.get(department=world.dept, module_key="training").enabled is False
    call_command("department_modules", "--show", "--all")
    out = capsys.readouterr().out
    assert world.dept.code in out and "off" in out
    call_command("department_modules", "--history")
    assert "Not in scope" in capsys.readouterr().out
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command(
            "department_modules", "--set", world.dept.code, "dsa", "--off", "--reason", "Nope nope", "--actor",
            world.student.email,
        )
