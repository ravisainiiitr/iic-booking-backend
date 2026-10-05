import io

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from iic_booking.procurement_management import access, config_service
from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.errors import ProcurementError
from iic_booking.procurement_management.models import ProcurementRoleAssignment
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, config_of, make_department, make_user, new_request

pytestmark = pytest.mark.django_db

ALLOWED = [UserType.ADMIN, UserType.DEPT_ADMIN, UserType.MANAGER, UserType.OPERATOR, UserType.FINANCE, UserType.OC_STORES, UserType.HOD]
DISALLOWED = [
    UserType.STUDENT,
    UserType.INDIVIDUAL_STUDENT,
    UserType.FACULTY,
    UserType.EXTERNAL,
    UserType.RND,
    UserType.INSTITUTE,
    UserType.STARTUP_INCUBATED_IITR,
    UserType.EXTERNAL_STARTUP_MSME,
    UserType.OTHER,
    UserType.ORG_ADMIN,
    UserType.EXTERNAL_RELATIONS,
    None,
]


def disabled(res):
    return res.status_code == 403 and res.json().get("code") == c.DISABLED_CODE


class TestAccessRule:
    def test_module_user_types(self):
        assert set(access.MODULE_USER_TYPES) == set(ALLOWED)

    @pytest.mark.parametrize("code", ALLOWED)
    def test_staff_types_with_a_role_can_use_module(self, world, code):
        user = make_user(user_type=code, department=world.dept)
        if code != UserType.ADMIN:
            ProcurementRoleAssignment.objects.create(department=world.dept, user=user, role=c.ModuleRole.OFFICE, permissions=[])
        body = client_for(user).get(f"{API}/bootstrap/").json()
        assert body["enabled"] is True

    @pytest.mark.parametrize("code", DISALLOWED)
    def test_other_types_are_refused_even_with_a_role(self, world, code):
        user = make_user(user_type=code, department=world.dept)
        ProcurementRoleAssignment.objects.create(department=world.dept, user=user, role=c.ModuleRole.OFFICE, permissions=list(c.ALL_OFFICE_PERMISSIONS))
        body = client_for(user).get(f"{API}/bootstrap/").json()
        assert body["enabled"] is False and body["can_configure"] is False and body["departments"] == []
        for url in ("requests/", "approvals/", "reports/", "dashboard/", "assets/", "config/"):
            assert disabled(client_for(user).get(f"{API}/{url}")), (code, url)

    def test_faculty_department_head_is_refused(self, world):
        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        world.dept.head = prof
        world.dept.save(update_fields=["head"])
        assert client_for(prof).get(f"{API}/bootstrap/").json()["enabled"] is False
        assert disabled(client_for(prof).get(f"{API}/requests/"))

    def test_faculty_oic_of_equipment_is_refused(self, world):
        from iic_booking.equipment.models import EquipmentManager

        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        EquipmentManager.objects.create(equipment=world.equipment, manager=prof)
        assert disabled(client_for(prof).get(f"{API}/requests/"))

    def test_superuser_counts_as_main_admin(self, world):
        su = make_user(user_type=UserType.STUDENT, is_superuser=True, is_staff=True)
        assert client_for(su).get(f"{API}/bootstrap/").json()["can_configure"] is True

    def test_pilot_and_type_rule_combine(self, world):
        config_service.update_config(world.admin, world.other_dept, {"module_enabled": False})
        student = make_user(user_type=UserType.STUDENT, department=world.dept)
        ProcurementRoleAssignment.objects.create(department=world.dept, user=student, role=c.ModuleRole.OFFICE, permissions=[])
        config_service.update_config(world.admin, world.dept, {"pilot_mode": True, "pilot_user_ids": [world.oic.pk]})
        assert client_for(world.oic).get(f"{API}/bootstrap/").json()["enabled"] is True
        assert client_for(world.oic2).get(f"{API}/bootstrap/").json()["enabled"] is False
        assert client_for(student).get(f"{API}/bootstrap/").json()["enabled"] is False


class TestServiceRefusesDisallowedTypes:
    def test_role_assignment_refused(self, world):
        student = make_user(user_type=UserType.STUDENT, department=world.dept)
        with pytest.raises(ProcurementError) as exc:
            config_service.assign_role(world.admin, world.dept, student, c.ModuleRole.OFFICE)
        assert exc.value.code == "user_type_not_allowed"
        res = client_for(world.admin).post(
            f"{API}/config/{world.dept.pk}/roles/", {"user_id": student.pk, "role": c.ModuleRole.OFFICE}, format="json"
        )
        assert res.status_code == 400 and res.json()["code"] == "user_type_not_allowed"

    def test_role_assignment_allowed_for_new_types(self, world):
        stores = make_user(user_type=UserType.OC_STORES, department=world.dept)
        hod = make_user(user_type=UserType.HOD, department=world.dept)
        config_service.assign_role(world.admin, world.dept, stores, c.ModuleRole.OC_STORES)
        config_service.assign_role(world.admin, world.dept, hod, c.ModuleRole.HOD)
        assert c.ModuleRole.OC_STORES in client_for(stores).get(f"{API}/bootstrap/").json()["departments"][0]["roles"]
        assert c.ModuleRole.HOD in client_for(hod).get(f"{API}/bootstrap/").json()["departments"][0]["roles"]

    def test_pilot_list_refuses_disallowed_types(self, world):
        faculty = make_user(user_type=UserType.FACULTY, department=world.dept)
        with pytest.raises(ProcurementError) as exc:
            config_service.update_config(world.admin, world.dept, {"pilot_user_ids": [world.oic.pk, faculty.pk]})
        assert exc.value.code == "user_type_not_allowed"
        assert not config_of(world.dept).pilot_users.exists()

    def test_user_search_lists_only_eligible_types(self, world):
        make_user(user_type=UserType.STUDENT, name="Zeta Searchable Student")
        stores = make_user(user_type=UserType.OC_STORES, name="Zeta Searchable Stores")
        rows = client_for(world.admin).get(f"{API}/config/users/", {"q": "Zeta Searchable"}).json()["results"]
        assert [r["id"] for r in rows] == [stores.pk]

    def test_disallowed_types_are_not_notified(self, world, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "iic_booking.communication.in_app.notify_in_app", lambda recipients, **kw: calls.append({u.pk for u in recipients})
        )
        from iic_booking.equipment.models import EquipmentManager

        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        EquipmentManager.objects.create(equipment=world.equipment, manager=prof)
        new_request(world.operator, equipment=world.equipment, submit=True)
        assert any(world.oic.pk in to for to in calls)
        assert all(prof.pk not in to for to in calls)


class TestNewUserTypes:
    @pytest.mark.parametrize("code,label", [(UserType.OC_STORES, "Officer In Charge Stores"), (UserType.HOD, "Head of Department")])
    def test_choice_exists(self, code, label):
        assert dict(UserType.get_choices())[code] == label

    @pytest.mark.parametrize("code", [UserType.OC_STORES, UserType.HOD])
    def test_treated_as_procurement_only_staff(self, code):
        assert code not in UserType.get_admin_panel_codes()
        assert code not in UserType.get_wallet_eligible_codes()
        assert code not in UserType.get_omniport_codes()
        assert code not in UserType.get_internal_user_codes()
        assert code not in UserType.get_external_user_codes()
        assert code not in UserType.get_management_user_codes()
        assert not UserType.is_end_user_booking_type(code)

    @pytest.mark.parametrize("code", [UserType.OC_STORES, UserType.HOD])
    def test_no_wallet_and_password_login_works(self, db, code):
        from iic_booking.users.models.wallet import Wallet

        dept = make_department()
        user = make_user(user_type=code, department=dept, email=f"pm-login-{code}@iic-booking.test")
        user.set_password("Pilot-Login-123!")
        user.save()
        assert not Wallet.objects.filter(user=user).exists()
        res = client_for().post("/api/auth/login/", {"email": user.email, "password": "Pilot-Login-123!"}, format="json")
        assert res.status_code == 200, res.json()
        assert res.json().get("token")

    @pytest.mark.parametrize("code", [UserType.OC_STORES, UserType.HOD])
    def test_cannot_self_register(self, db, code):
        res = client_for().post(
            "/api/auth/register/",
            {"email": f"self-{code}@example.org", "password": "Some-Password-123!", "name": "Self", "user_type": code},
            format="json",
        )
        assert res.status_code == 400
        assert not User.objects.filter(email=f"self-{code}@example.org").exists()


class TestPilotAccountsCommand:
    def test_creates_flagged_accounts_without_wallets(self, db, monkeypatch):
        from iic_booking.users.models.wallet import Wallet

        dept = make_department()
        pw = {"finance": "Finance-Pilot-123!", "oc_stores": "Stores-Pilot-123!", "hod": "Hod-Pilot-1234!"}
        monkeypatch.setattr("sys.stdin", io.StringIO(__import__("json").dumps(pw)))
        out = io.StringIO()
        call_command("procurement_pilot_accounts", "--department-id", str(dept.pk), "--types", "finance", "oc_stores", "hod", stdout=out)
        text = out.getvalue()
        for secret in pw.values():
            assert secret not in text
        for code, name in [("finance", "Test Accounts In Charge"), ("oc_stores", "Test Officer In Charge Stores"), ("hod", "Test Head of Department")]:
            user = User.objects.get(email=f"test.{code}@iic-booking.test")
            assert user.name == name and user.user_type == code and user.department_id == dept.pk
            assert user.is_test_account and user.is_active and user.check_password(pw[code])
            assert not Wallet.objects.filter(user=user).exists()
            assert f"PILOT_ACCOUNT created id={user.pk}" in text

        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        out = io.StringIO()
        call_command("procurement_pilot_accounts", "--department-id", str(dept.pk), "--types", "hod", stdout=out)
        assert "PILOT_ACCOUNT reused" in out.getvalue()
        assert User.objects.get(email="test.hod@iic-booking.test").check_password(pw["hod"])

    def test_refuses_non_staff_types_and_short_passwords(self, db, monkeypatch):
        dept = make_department()
        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        with pytest.raises(CommandError):
            call_command("procurement_pilot_accounts", "--department-id", str(dept.pk), "--types", "student")
        monkeypatch.setattr("sys.stdin", io.StringIO('{"hod": "short"}'))
        with pytest.raises(CommandError):
            call_command("procurement_pilot_accounts", "--department-id", str(dept.pk), "--types", "hod")
        assert not User.objects.filter(email="test.hod@iic-booking.test").exists()
