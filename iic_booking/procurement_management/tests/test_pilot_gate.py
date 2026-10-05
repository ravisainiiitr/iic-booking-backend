import pytest

from iic_booking.procurement_management import config_service
from iic_booking.procurement_management import constants as c
from iic_booking.procurement_management.errors import ProcurementError
from iic_booking.procurement_management.models import ProcurementAuditLog
from iic_booking.users.models.user_type import UserType

from .conftest import API, act, client_for, config_of, make_department, make_equipment, make_user, new_request, pdf_upload

pytestmark = pytest.mark.django_db
RS = c.RequestStatus


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake(recipients, **kwargs):
        calls.append({"to": {u.pk for u in recipients}, **kwargs})

    monkeypatch.setattr("iic_booking.communication.in_app.notify_in_app", fake)
    return calls


def pilot(world, *users, dept=None):
    return config_service.update_config(
        world.admin,
        dept or world.dept,
        {"pilot_mode": True, "pilot_user_ids": [u.pk for u in users], "reason": "Pilot testing"},
    )


@pytest.fixture
def piloted(world):
    """``world.dept`` in pilot mode with only operator, OIC and OC Stores allowed; the other department off."""
    r = new_request(world.operator, equipment=world.equipment, submit=True)
    up = client_for(world.operator).post(
        f"{API}/requests/{r.pk}/documents/", {"file": pdf_upload(), "doc_type": "QUOTATION"}, format="multipart"
    )
    assert up.status_code == 201, up.json()
    config_service.update_config(world.admin, world.other_dept, {"module_enabled": False})
    pilot(world, world.operator, world.oic, world.stores)
    world.request = r
    world.doc_id = up.json()["id"]
    world.superuser = make_user(user_type=UserType.ADMIN, name="Super User", is_superuser=True, is_staff=True)
    return world


def blocked_users(w):
    return [w.admin, w.superuser, w.oic2, w.operator2, w.office, w.hod, w.auditor, w.outsider]


def disabled(res):
    return res.status_code == 403 and res.json().get("code") == c.DISABLED_CODE


class TestDefaults:
    def test_new_config_is_pilot_with_nobody_allowed(self, db):
        dept = make_department()
        eq = make_equipment(dept)
        from iic_booking.equipment.models import EquipmentManager

        admin = make_user(user_type=UserType.ADMIN)
        oic = make_user(user_type=UserType.MANAGER, department=dept)
        EquipmentManager.objects.create(equipment=eq, manager=oic)
        cfg = config_service.update_config(admin, dept, {"module_enabled": True})
        assert cfg.pilot_mode is True
        assert not cfg.pilot_users.exists()
        for user in (admin, oic):
            body = client_for(user).get(f"{API}/bootstrap/").json()
            assert body["enabled"] is False
            assert body["can_configure"] is False
            assert body["departments"] == [] and body["menus"] == {} and body["equipment"] == []
            assert disabled(client_for(user).get(f"{API}/config/"))
            assert disabled(client_for(user).get(f"{API}/requests/"))

    def test_config_serializer_exposes_pilot_fields(self, piloted):
        pilot(piloted, piloted.operator, piloted.oic, piloted.stores, piloted.admin)
        body = client_for(piloted.admin).get(f"{API}/config/{piloted.dept.pk}/").json()
        assert body["pilot_mode"] is True
        assert sorted(u["id"] for u in body["pilot_users"]) == sorted(
            [piloted.operator.pk, piloted.oic.pk, piloted.stores.pk, piloted.admin.pk]
        )


class TestNonListedUsersBlocked:
    def calls(self, w):
        r, dept = w.request, w.dept.pk
        return [
            ("get", f"{API}/requests/", None),
            ("get", f"{API}/requests/{r.pk}/", None),
            ("post", f"{API}/requests/{r.pk}/approve/", {"comments": "x"}),
            ("post", f"{API}/requests/{r.pk}/cancel/", {"reason": "x"}),
            ("get", f"{API}/requests/{r.pk}/documents/", None),
            ("get", f"{API}/documents/{w.doc_id}/download/", None),
            ("get", f"{API}/approvals/", None),
            ("get", f"{API}/reports/", None),
            ("get", f"{API}/reports/requests/", None),
            ("get", f"{API}/dashboard/", None),
            ("get", f"{API}/assets/", None),
            ("get", f"{API}/stock/balances/", None),
            ("get", f"{API}/amc/", None),
            ("get", f"{API}/items/", None),
            ("get", f"{API}/audit/", None),
            ("get", f"{API}/config/", None),
            ("get", f"{API}/config/{dept}/", None),
            ("patch", f"{API}/config/{dept}/", {"pilot_mode": False, "reason": "x"}),
            ("get", f"{API}/config/{dept}/roles/", None),
            ("post", f"{API}/config/{dept}/roles/", {"user_id": w.outsider.pk, "role": c.ModuleRole.OFFICE}),
            ("get", f"{API}/config/users/", None),
        ]

    def test_every_endpoint_refuses_non_listed_users(self, piloted):
        for user in blocked_users(piloted):
            cl = client_for(user)
            for method, url, body in self.calls(piloted):
                res = getattr(cl, method)(url, body, format="json") if body is not None else getattr(cl, method)(url)
                assert disabled(res), (user.name, method, url, res.status_code)
        piloted.request.refresh_from_db()
        assert piloted.request.status == RS.PENDING_OIC
        assert config_of(piloted.dept).pilot_mode is True

    def test_bootstrap_reports_disabled_for_non_listed_users(self, piloted):
        for user in blocked_users(piloted):
            res = client_for(user).get(f"{API}/bootstrap/")
            assert res.status_code == 200
            body = res.json()
            assert body["enabled"] is False, user.name
            assert body["can_configure"] is False, user.name
            assert body["departments"] == [] and body["menus"] == {} and body["equipment"] == []

    def test_admin_is_blocked_unless_listed(self, piloted):
        assert client_for(piloted.admin).get(f"{API}/bootstrap/").json()["can_configure"] is False
        pilot(piloted, piloted.operator, piloted.oic, piloted.stores, piloted.admin)
        body = client_for(piloted.admin).get(f"{API}/bootstrap/").json()
        assert body["can_configure"] is True
        assert client_for(piloted.admin).get(f"{API}/config/").status_code == 200


class TestPilotUsers:
    def test_pilot_users_work_normally(self, piloted):
        r = piloted.request
        body = client_for(piloted.operator).get(f"{API}/bootstrap/").json()
        assert body["enabled"] is True
        assert [d["department"]["id"] for d in body["departments"]] == [piloted.dept.pk]
        assert client_for(piloted.operator).get(f"{API}/requests/").json()["count"] == 1
        assert client_for(piloted.operator).get(f"{API}/documents/{piloted.doc_id}/download/").status_code == 200
        act(piloted.oic, r, "approve", comments="ok")
        assert r.status == RS.PENDING_STORES
        act(piloted.stores, r, "approve")
        assert r.status == RS.APPROVED

    def test_department_counts_only_where_user_is_listed(self, piloted):
        config_service.update_config(
            piloted.admin, piloted.other_dept, {"module_enabled": True, "pilot_mode": True, "pilot_user_ids": [piloted.other_oic.pk]}
        )
        op = client_for(piloted.operator).get(f"{API}/bootstrap/").json()
        assert [d["department"]["id"] for d in op["departments"]] == [piloted.dept.pk]
        other = client_for(piloted.other_oic).get(f"{API}/bootstrap/").json()
        assert [d["department"]["id"] for d in other["departments"]] == [piloted.other_dept.pk]
        assert client_for(piloted.other_oic).get(f"{API}/requests/{piloted.request.pk}/").status_code == 404

    def test_turning_pilot_off_opens_module_by_role(self, piloted):
        config_service.update_config(piloted.admin, piloted.dept, {"pilot_mode": False, "reason": "Go live"})
        assert client_for(piloted.oic2).get(f"{API}/bootstrap/").json()["enabled"] is True
        assert client_for(piloted.admin).get(f"{API}/bootstrap/").json()["can_configure"] is True
        assert client_for(piloted.outsider).get(f"{API}/bootstrap/").json()["enabled"] is False

    def test_disabling_module_blocks_pilot_users(self, piloted):
        config_service.update_config(piloted.admin, piloted.dept, {"module_enabled": False, "reason": "Pause"})
        assert client_for(piloted.operator).get(f"{API}/bootstrap/").json()["enabled"] is False
        assert disabled(client_for(piloted.operator).get(f"{API}/requests/"))


class TestPilotNotifications:
    def test_only_pilot_users_are_notified(self, piloted, sent):
        pilot(piloted, piloted.operator, piloted.stores)
        new_request(piloted.operator, equipment=piloted.equipment, submit=True)
        assert all(piloted.oic.pk not in call["to"] for call in sent)
        sent.clear()
        pilot(piloted, piloted.operator, piloted.oic, piloted.stores)
        new_request(piloted.operator, equipment=piloted.equipment, submit=True)
        assert any(piloted.oic.pk in call["to"] for call in sent)


class TestPilotConfigAudit:
    def test_pilot_user_changes_are_audited(self, piloted):
        log = ProcurementAuditLog.objects.filter(department=piloted.dept, action="config.pilot_users").latest("id")
        assert log.actor == piloted.admin
        assert log.reason == "Pilot testing"
        assert log.new_value["pilot_user_ids"] == sorted([piloted.operator.pk, piloted.oic.pk, piloted.stores.pk])
        count = ProcurementAuditLog.objects.filter(action="config.pilot_users").count()
        pilot(piloted, piloted.stores, piloted.oic, piloted.operator)
        assert ProcurementAuditLog.objects.filter(action="config.pilot_users").count() == count

    def test_pilot_mode_change_is_audited(self, piloted):
        log = ProcurementAuditLog.objects.filter(department=piloted.dept, action="config.updated").latest("id")
        assert log.old_value == {"pilot_mode": False} and log.new_value == {"pilot_mode": True}

    @pytest.mark.parametrize("ids", ["1", [{"x": 1}], ["abc"], [987654321]])
    def test_invalid_pilot_users_rejected(self, world, ids):
        with pytest.raises(ProcurementError) as exc:
            config_service.update_config(world.admin, world.dept, {"pilot_user_ids": ids})
        assert exc.value.code == "invalid_pilot_users"

    def test_inactive_user_rejected(self, world):
        from iic_booking.users.models import User

        gone = make_user()
        User.objects.filter(pk=gone.pk).update(is_active=False)
        with pytest.raises(ProcurementError):
            config_service.update_config(world.admin, world.dept, {"pilot_user_ids": [gone.pk]})

    def test_only_main_admin_sets_pilot_users(self, world):
        with pytest.raises(Exception):
            config_service.update_config(world.office, world.dept, {"pilot_user_ids": [world.office.pk]})
        assert not config_of(world.dept).pilot_users.exists()

    def test_listed_admin_sets_pilot_users_via_api(self, piloted):
        pilot(piloted, piloted.operator, piloted.oic, piloted.stores, piloted.admin)
        res = client_for(piloted.admin).patch(
            f"{API}/config/{piloted.dept.pk}/",
            {"pilot_user_ids": [piloted.admin.pk, piloted.oic2.pk], "reason": "Add tester"},
            format="json",
        )
        assert res.status_code == 200, res.json()
        assert sorted(u["id"] for u in res.json()["pilot_users"]) == sorted([piloted.admin.pk, piloted.oic2.pk])
        assert client_for(piloted.oic2).get(f"{API}/bootstrap/").json()["enabled"] is True
        assert disabled(client_for(piloted.operator).get(f"{API}/requests/"))
