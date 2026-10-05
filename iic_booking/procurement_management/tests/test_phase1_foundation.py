from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal

import pytest
from django.db import connection

from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.fy import fy_bounds, fy_label, is_valid_fy_label
from iic_booking.procurement_management.models import (
    ImmutableRecordError,
    ProcurementAuditLog,
    ProcurementManagementConfiguration,
    ProcurementRoleAssignment,
)
from iic_booking.procurement_management.numbering import next_number
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, make_department, make_equipment, make_user

pytestmark = pytest.mark.django_db


class TestFeatureFlagDefaultsOff:
    def test_new_department_has_module_off(self):
        dept = make_department()
        assert not ProcurementManagementConfiguration.objects.filter(department=dept).exists()
        cfg = ProcurementManagementConfiguration(department=dept)
        assert cfg.module_enabled is False
        assert cfg.small_purchase_threshold == Decimal("2000.00")

    def test_bootstrap_disabled_for_everyone_when_off(self):
        dept = make_department()
        eq = make_equipment(dept)
        from iic_booking.equipment.models import EquipmentManager

        oic = make_user(user_type=UserType.MANAGER)
        EquipmentManager.objects.create(equipment=eq, manager=oic)
        res = client_for(oic).get(f"{API}/bootstrap/")
        assert res.status_code == 200
        assert res.json()["enabled"] is False
        assert res.json()["menus"] == {}

    def test_module_apis_refuse_when_off(self):
        oic = make_user(user_type=UserType.MANAGER)
        res = client_for(oic).get(f"{API}/audit/")
        assert res.status_code == 403
        assert res.json()["code"] == c.DISABLED_CODE

    def test_anonymous_refused(self):
        assert client_for().get(f"{API}/bootstrap/").status_code in (401, 403)

    def test_disabled_department_hidden_even_with_roles(self, world):
        world.cfg.module_enabled = False
        world.cfg.save()
        body = client_for(world.oic).get(f"{API}/bootstrap/").json()
        assert body["enabled"] is False

    def test_enabled_department_visible_to_oic(self, world):
        body = client_for(world.oic).get(f"{API}/bootstrap/").json()
        assert body["enabled"] is True
        [dept] = body["departments"]
        assert dept["department"]["id"] == world.dept.pk
        assert c.ModuleRole.OIC in dept["roles"]
        assert dept["menus"]["approvals"] is True
        assert dept["menus"]["configuration"] is False
        assert [(e["id"], e["is_oic"]) for e in body["equipment"]] == [(world.equipment.pk, True)]

    def test_department_wide_roles_get_department_equipment(self, world):
        body = client_for(world.office).get(f"{API}/bootstrap/").json()
        assert sorted(e["id"] for e in body["equipment"]) == sorted([world.equipment.pk, world.equipment2.pk])
        assert not any(e["is_oic"] for e in body["equipment"])

    def test_student_without_role_sees_nothing(self, world):
        assert client_for(world.outsider).get(f"{API}/bootstrap/").json()["enabled"] is False


class TestConfiguration:
    def test_only_main_admin_can_enable(self, world):
        dept = make_department()
        res = client_for(world.oic).patch(f"{API}/config/{dept.pk}/", {"module_enabled": True}, format="json")
        assert res.status_code == 403
        assert not ProcurementManagementConfiguration.objects.filter(department=dept, module_enabled=True).exists()

    def test_admin_enables_and_change_is_audited(self, world):
        dept = make_department()
        res = client_for(world.admin).patch(
            f"{API}/config/{dept.pk}/",
            {"module_enabled": True, "small_purchase_threshold": "2500.00", "reason": "pilot"},
            format="json",
        )
        assert res.status_code == 200, res.json()
        assert res.json()["module_enabled"] is True
        assert res.json()["small_purchase_threshold"] == "2500.00"
        log = ProcurementAuditLog.objects.filter(department=dept, action="config.module_enabled").get()
        assert log.actor == world.admin
        assert log.old_value["module_enabled"] is False
        assert log.new_value["small_purchase_threshold"] == "2500.00"
        assert log.reason == "pilot"
        from iic_booking.procurement_management.models import GSTRate, ItemCategory, RequestTypeConfig

        assert ItemCategory.objects.filter(department=dept).count() == 7
        assert RequestTypeConfig.objects.filter(department=dept).count() == 12
        assert GSTRate.objects.filter(department=dept).exists()

    def test_negative_threshold_rejected(self, world):
        res = client_for(world.admin).patch(
            f"{API}/config/{world.dept.pk}/", {"small_purchase_threshold": "-1"}, format="json"
        )
        assert res.status_code == 400

    def test_admin_list_shows_all_internal_departments(self, world):
        res = client_for(world.admin).get(f"{API}/config/")
        ids = {row["department"]["id"] for row in res.json()["results"]}
        assert {world.dept.pk, world.other_dept.pk} <= ids


class TestRoles:
    def test_assign_and_revoke_role_audited(self, world):
        user = make_user(user_type=UserType.FINANCE)
        res = client_for(world.admin).post(
            f"{API}/config/{world.dept.pk}/roles/",
            {"user_id": user.pk, "role": "OFFICE", "permissions": ["record_small_purchase", "invoices"]},
            format="json",
        )
        assert res.status_code == 201, res.json()
        row = ProcurementRoleAssignment.objects.get(pk=res.json()["id"])
        assert row.permissions == ["invoices", "record_small_purchase"]
        body = client_for(user).get(f"{API}/bootstrap/").json()
        assert body["departments"][0]["permissions"] == ["invoices", "record_small_purchase"]
        res = client_for(world.admin).delete(f"{API}/config/{world.dept.pk}/roles/{row.pk}/")
        assert res.status_code == 204
        assert client_for(user).get(f"{API}/bootstrap/").json()["enabled"] is False
        assert ProcurementAuditLog.objects.filter(action="role.revoked", object_id=str(row.pk)).exists()

    def test_unknown_permission_rejected(self, world):
        user = make_user()
        res = client_for(world.admin).post(
            f"{API}/config/{world.dept.pk}/roles/", {"user_id": user.pk, "role": "OFFICE", "permissions": ["god_mode"]},
            format="json",
        )
        assert res.status_code == 400

    def test_non_admin_cannot_assign(self, world):
        res = client_for(world.office).post(
            f"{API}/config/{world.dept.pk}/roles/", {"user_id": world.outsider.pk, "role": "OFFICE"}, format="json"
        )
        assert res.status_code == 403

    def test_hod_from_department_head(self, world):
        body = client_for(world.hod).get(f"{API}/bootstrap/").json()
        assert c.ModuleRole.HOD in body["departments"][0]["roles"]

    def test_temporary_oic_counts_as_oic(self, world, temp_oic):
        body = client_for(temp_oic).get(f"{API}/bootstrap/").json()
        assert c.ModuleRole.OIC in body["departments"][0]["roles"]

    def test_roles_scoped_to_department(self, world):
        body = client_for(world.other_oic).get(f"{API}/bootstrap/").json()
        assert [d["department"]["id"] for d in body["departments"]] == [world.other_dept.pk]


class TestNumberingAndFY:
    def test_fy_label_and_bounds(self):
        assert fy_label(date(2026, 4, 1)) == "2026-27"
        assert fy_label(date(2027, 3, 31)) == "2026-27"
        assert fy_label(date(2026, 3, 31)) == "2025-26"
        assert fy_bounds("2026-27") == (date(2026, 4, 1), date(2027, 3, 31))
        assert is_valid_fy_label("2026-27")
        assert not is_valid_fy_label("2026-28")
        assert not is_valid_fy_label("26-27")

    def test_number_format_and_sequence(self):
        a = next_number("REQ", financial_year="2026-27")
        b = next_number("REQ", financial_year="2026-27")
        other = next_number("REQ", financial_year="2027-28")
        assert a == "REQ/2026-27/00001"
        assert b == "REQ/2026-27/00002"
        assert other == "REQ/2027-28/00001"

    @pytest.mark.django_db(transaction=True)
    def test_concurrent_numbers_unique(self):
        def take(_):
            try:
                return next_number("PROC", financial_year="2030-31")
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            numbers = list(pool.map(take, range(24)))
        assert len(set(numbers)) == 24
        assert sorted(numbers)[-1] == "PROC/2030-31/00024"


class TestAppendOnly:
    def test_audit_log_cannot_be_modified_or_deleted(self, world):
        log = ProcurementAuditLog.objects.filter(department=world.dept).first()
        log.reason = "tamper"
        with pytest.raises(ImmutableRecordError):
            log.save()
        with pytest.raises(ImmutableRecordError):
            log.delete()
        with pytest.raises(ImmutableRecordError):
            ProcurementAuditLog.objects.filter(pk=log.pk).update(reason="x")
        with pytest.raises(ImmutableRecordError):
            ProcurementAuditLog.objects.filter(pk=log.pk).delete()


class TestDjangoAdmin:
    def test_every_model_is_registered_read_only(self, rf):
        from django.apps import apps
        from django.contrib import admin

        request = rf.get("/")
        request.user = make_user(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)
        for model in apps.get_app_config("procurement_management").get_models():
            model_admin = admin.site._registry.get(model)
            assert model_admin is not None, model.__name__
            assert not model_admin.has_add_permission(request)
            assert not model_admin.has_change_permission(request)
            assert not model_admin.has_delete_permission(request)
            assert model_admin.has_view_permission(request)
