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

from .conftest import API, act, category, client_for, config_of, line, make_department, make_user, new_request

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


def make_hod_assignment(user, department, *, active=True, start_days=-30, end_days=None):
    from datetime import timedelta

    from django.utils import timezone

    from iic_booking.users.models.channel_i_identity import HeadOfDepartmentAssignment

    today = timezone.localdate()
    return HeadOfDepartmentAssignment.objects.create(
        user=user,
        department=department,
        active=active,
        effective_from=today + timedelta(days=start_days),
        effective_to=None if end_days is None else today + timedelta(days=end_days),
    )


def dept_roles(user):
    body = client_for(user).get(f"{API}/bootstrap/").json()
    return body, {d["department"]["id"]: set(d["roles"]) for d in body["departments"]}


class TestHeadOfDepartmentException:
    """Any account type heading a department may use the module there, and only there."""

    @pytest.mark.parametrize("code", [UserType.FACULTY, UserType.STUDENT, UserType.EXTERNAL, None])
    def test_hod_assignment_allows_any_type_in_that_department_only(self, world, code):
        prof = make_user(user_type=code, department=world.dept)
        make_hod_assignment(prof, world.dept)
        body, roles = dept_roles(prof)
        assert body["enabled"] is True and body["can_configure"] is False
        assert roles == {world.dept.pk: {c.ModuleRole.HOD}}
        for url in ("requests/", "approvals/", "reports/", "dashboard/", "assets/"):
            assert client_for(prof).get(f"{API}/{url}").status_code == 200, url
        assert client_for(prof).get(f"{API}/dashboard/", {"department_id": world.dept.pk}).status_code == 200
        assert client_for(prof).get(f"{API}/dashboard/", {"department_id": world.other_dept.pk}).status_code == 404

    def test_department_head_counts_when_department_has_no_assignment(self, world):
        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        world.dept.head = prof
        world.dept.save(update_fields=["head"])
        assert dept_roles(prof)[1] == {world.dept.pk: {c.ModuleRole.HOD}}

    def test_stale_department_head_is_ignored_once_an_assignment_exists(self, world):
        old = make_user(user_type=UserType.FACULTY, department=world.dept)
        new = make_user(user_type=UserType.FACULTY, department=world.dept)
        world.dept.head = old
        world.dept.save(update_fields=["head"])
        make_hod_assignment(new, world.dept)
        assert client_for(old).get(f"{API}/bootstrap/").json()["enabled"] is False
        assert disabled(client_for(old).get(f"{API}/requests/"))
        assert dept_roles(new)[1] == {world.dept.pk: {c.ModuleRole.HOD}}

    @pytest.mark.parametrize(
        "kwargs", [{"active": False}, {"end_days": -1}, {"start_days": 1}], ids=["inactive", "ended", "not_started"]
    )
    def test_non_current_assignment_does_not_count(self, world, kwargs):
        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        make_hod_assignment(prof, world.dept, **kwargs)
        assert client_for(prof).get(f"{API}/bootstrap/").json()["enabled"] is False
        assert disabled(client_for(prof).get(f"{API}/requests/"))

    def test_roles_elsewhere_do_not_leak(self, world):
        from iic_booking.equipment.models import EquipmentManager

        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        make_hod_assignment(prof, world.dept)
        EquipmentManager.objects.create(equipment=world.other_equipment, manager=prof)
        ProcurementRoleAssignment.objects.create(department=world.other_dept, user=prof, role=c.ModuleRole.OFFICE, permissions=[])
        assert dept_roles(prof)[1] == {world.dept.pk: {c.ModuleRole.HOD}}

    def test_department_switch_still_applies(self, world):
        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        make_hod_assignment(prof, world.dept)
        config_service.update_config(world.admin, world.dept, {"module_enabled": False})
        body = client_for(prof).get(f"{API}/bootstrap/").json()
        assert body["enabled"] is False and body["departments"] == []
        for url in ("requests/", "config/"):
            assert disabled(client_for(prof).get(f"{API}/{url}")), url

    def test_pilot_allowlist_still_applies(self, world):
        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        make_hod_assignment(prof, world.dept)
        config_service.update_config(world.admin, world.other_dept, {"module_enabled": False})
        config_service.update_config(world.admin, world.dept, {"pilot_mode": True, "pilot_user_ids": [world.oic.pk]})
        assert disabled(client_for(prof).get(f"{API}/requests/"))
        assert client_for(prof).get(f"{API}/bootstrap/").json()["enabled"] is False
        config_service.update_config(world.admin, world.dept, {"pilot_user_ids": [world.oic.pk, prof.pk]})
        assert dept_roles(prof)[1] == {world.dept.pk: {c.ModuleRole.HOD}}

    def test_pilot_list_and_roles_accept_the_hod_only_for_their_department(self, world):
        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        make_hod_assignment(prof, world.dept)
        config_service.update_config(world.admin, world.dept, {"pilot_user_ids": [prof.pk]})
        config_service.assign_role(world.admin, world.dept, prof, c.ModuleRole.HOD)
        with pytest.raises(ProcurementError) as exc:
            config_service.update_config(world.admin, world.other_dept, {"pilot_user_ids": [prof.pk]})
        assert exc.value.code == "user_type_not_allowed"
        with pytest.raises(ProcurementError) as exc:
            config_service.assign_role(world.admin, world.other_dept, prof, c.ModuleRole.OFFICE)
        assert exc.value.code == "user_type_not_allowed"

    def test_user_search_includes_department_heads(self, world):
        prof = make_user(user_type=UserType.FACULTY, name="Zeta Head Prof")
        make_user(user_type=UserType.FACULTY, name="Zeta Plain Prof")
        make_hod_assignment(prof, world.dept)
        rows = client_for(world.admin).get(f"{API}/config/users/", {"q": "Zeta"}).json()["results"]
        assert [r["id"] for r in rows] == [prof.pk]

    def test_audience_and_hod_stage_notification(self, world, monkeypatch):
        prof = make_user(user_type=UserType.FACULTY, department=world.dept)
        make_hod_assignment(prof, world.dept)
        assert access.pilot_audience(world.dept.pk, [prof]) == [prof]
        assert access.pilot_audience(world.other_dept.pk, [prof]) == []
        calls = []
        monkeypatch.setattr(
            "iic_booking.communication.in_app.notify_in_app", lambda recipients, **kw: calls.append({u.pk for u in recipients})
        )
        r = new_request(
            world.operator, equipment=world.equipment, rt="MAJOR_ASSET", cat=category(world.dept, "MAJOR_ASSET"),
            specification="Spec", lines=[line("100.00")], submit=True,
        )
        act(world.oic, r, "approve")
        act(world.stores, r, "approve")
        assert any(prof.pk in to for to in calls)
        assert all(world.hod.pk not in to for to in calls)
        act(prof, r, "approve")
        assert r.status == "APPROVED"


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
