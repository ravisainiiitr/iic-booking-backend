from datetime import timedelta
from io import StringIO
from unittest import mock

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from iic_booking.training import access
from iic_booking.training.models import (
    TrainingAudience,
    TrainingAuditLog,
    TrainingEquipmentSetting,
    TrainingModuleSettings,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, at, client_for, future_day, make_user


def demo_payload(equipment):
    return {
        "equipment_id": equipment.equipment_id,
        "purpose": "COURSE",
        "course_code": "CY-101",
        "participants_requested": 2,
        "requested_duration_minutes": 60,
        "preferred_windows": [{"start": at(future_day(), 10).isoformat(), "end": at(future_day(), 11).isoformat()}],
    }


def set_audience(value):
    TrainingModuleSettings.objects.update_or_create(pk=1, defaults={"audience": value})


@pytest.fixture
def admin_only_db(training_settings):
    """Env switch off: everything comes from the Main Admin settings."""
    training_settings.TRAINING_MODULE_ENABLED = False
    return training_settings


# ---------------------------------------------------------------------------
# Main Admin only
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_module_settings_api_is_main_admin_only(world):
    eq = world.equipment
    for user in (world.faculty, world.student, world.oic, world.operator):
        c = client_for(user)
        assert c.get(f"{API}/admin/module/").status_code == 403
        assert c.post(f"{API}/admin/module/", {"module_enabled": True}, format="json").status_code == 403
        assert c.get(f"{API}/admin/equipment/").status_code == 403
        assert c.post(f"{API}/admin/equipment/{eq.equipment_id}/", {"enabled": True}, format="json").status_code == 403
    dept_admin = make_user(user_type=UserType.DEPT_ADMIN, department=world.dept)
    assert client_for(dept_admin).post(f"{API}/admin/module/", {"module_enabled": True}, format="json").status_code == 403
    assert not TrainingEquipmentSetting.objects.exists()
    assert TrainingModuleSettings.current().module_enabled is False

    superuser = make_user(user_type=UserType.FACULTY, is_superuser=True, is_staff=True)
    assert client_for(superuser).get(f"{API}/admin/module/").status_code == 200
    assert client_for(world.admin).get(f"{API}/admin/module/").status_code == 200


@pytest.mark.django_db
def test_main_admin_turns_module_on_and_enables_equipment(world, admin_only_db):
    admin = client_for(world.admin)
    state = admin.get(f"{API}/admin/module/").data
    assert state["module_enabled"] is False and state["env_module_enabled"] is False

    state = admin.post(f"{API}/admin/module/", {"module_enabled": True, "audience": "EVERYONE"}, format="json").data
    assert state["module_enabled"] is True and state["db_module_enabled"] is True and state["audience"] == "EVERYONE"
    assert state["all_equipment_in_scope"] is False and state["enabled_equipment"] == []
    assert admin.post(f"{API}/admin/module/", {"audience": "NOPE"}, format="json").status_code == 400

    row = admin.post(f"{API}/admin/equipment/{world.equipment.equipment_id}/", {"enabled": True}, format="json").data
    assert row["enabled"] is True and row["training_active"] is True
    listing = admin.get(f"{API}/admin/equipment/", {"q": world.equipment.code}).data
    assert [r["enabled"] for r in listing["results"]] == [True]
    assert [r["code"] for r in admin.get(f"{API}/admin/equipment/", {"enabled": "1"}).data["results"]] == [world.equipment.code]
    assert TrainingAuditLog.objects.filter(action="equipment.training_enabled").count() == 1
    assert TrainingAuditLog.objects.filter(action="module.settings_updated").exists()

    admin.post(f"{API}/admin/equipment/{world.equipment.equipment_id}/", {"enabled": False}, format="json")
    assert access.pilot_equipment_ids() == set()


# ---------------------------------------------------------------------------
# Per-equipment gating
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_only_enabled_equipment_is_in_scope(world, admin_only_db):
    from iic_booking.training import module_config

    module_config.update_module(world.admin, module_enabled=True)
    assert client_for(world.faculty).get(f"{API}/equipment/").data["results"] == []
    assert client_for(world.oic).get(f"{API}/bootstrap/").data["menus"]["training_workspace"] is False

    module_config.set_equipment_enabled(world.admin, world.equipment, True)
    codes = [r["code"] for r in client_for(world.faculty).get(f"{API}/equipment/").data["results"]]
    assert codes == [world.equipment.code]
    assert client_for(world.faculty).post(f"{API}/demo-requests/", demo_payload(world.equipment), format="json").status_code == 201
    resp = client_for(world.faculty).post(f"{API}/demo-requests/", demo_payload(world.other_equipment), format="json")
    assert resp.status_code == 400 and resp.data["code"] == "not_in_pilot"
    assert client_for(world.oic).get(f"{API}/bootstrap/").data["menus"]["training_workspace"] is True
    assert client_for(world.other_oic).get(f"{API}/bootstrap/").data["menus"]["training_workspace"] is False


@pytest.mark.django_db
def test_env_pilot_codes_still_count_alongside_admin_list(world, training_settings):
    from iic_booking.training import module_config

    training_settings.TRAINING_PILOT_EQUIPMENT_CODES = world.other_equipment.code
    module_config.set_equipment_enabled(world.admin, world.equipment, True)
    assert access.pilot_equipment_ids() == {world.equipment.equipment_id, world.other_equipment.equipment_id}
    training_settings.TRAINING_PILOT_EQUIPMENT_CODES = ""
    assert access.pilot_equipment_ids() == {world.equipment.equipment_id}
    module_config.set_equipment_enabled(world.admin, world.equipment, False)
    assert access.pilot_equipment_ids() is None  # env switch on with no list anywhere: legacy all-equipment


@pytest.mark.django_db
def test_module_off_when_both_switches_off(world, admin_only_db):
    resp = client_for(world.faculty).get(f"{API}/demo-requests/")
    assert resp.status_code == 403 and resp.data["code"] == "training_disabled"
    assert client_for(world.faculty).get(f"{API}/bootstrap/").data["enabled"] is False


# ---------------------------------------------------------------------------
# Audience: test accounts only
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_test_only_audience_blocks_real_users_and_allows_test_users(world):
    set_audience(TrainingAudience.TEST_ACCOUNTS)
    test_faculty = make_user(user_type=UserType.FACULTY, department=world.dept, is_test_account=True)
    test_student = make_user(user_type=UserType.STUDENT, department=world.dept, supervisor=test_faculty, is_test_account=True)

    boot = client_for(world.faculty).get(f"{API}/bootstrap/").data
    assert boot["enabled"] is False and boot["audience"] == "TEST_ACCOUNTS"
    assert not any(boot["menus"].values())
    assert client_for(world.student).get(f"{API}/bootstrap/").data["menus"]["my_trainings"] is False
    for user, path in ((world.faculty, "demo-requests/"), (world.faculty, "calls/?scope=open"), (world.student, "me/trainings/")):
        resp = client_for(user).get(f"{API}/{path}")
        assert resp.status_code == 403 and resp.data["code"] == "training_not_available", path
    resp = client_for(world.faculty).post(f"{API}/demo-requests/", demo_payload(world.equipment), format="json")
    assert resp.status_code == 403
    assert client_for(world.faculty).get(f"{API}/badges/").data == {"results": {}}

    assert client_for(test_faculty).get(f"{API}/bootstrap/").data["menus"]["training_events"] is True
    assert client_for(test_student).get(f"{API}/bootstrap/").data["menus"]["my_trainings"] is True
    assert client_for(test_faculty).post(f"{API}/demo-requests/", demo_payload(world.equipment), format="json").status_code == 201
    assert client_for(test_student).get(f"{API}/me/trainings/").status_code == 200

    # Staff of enabled equipment keep the OIC / attendance side so the flow can be tested.
    assert client_for(world.oic).get(f"{API}/bootstrap/").data["menus"]["training_workspace"] is True
    assert client_for(world.operator).get(f"{API}/bootstrap/").data["menus"]["training_attendance"] is True
    assert client_for(world.oic).get(f"{API}/demo-requests/").status_code == 200


@pytest.mark.django_db
def test_test_only_audience_limits_nominations_and_call_broadcast(world):
    from iic_booking.training import selection

    set_audience(TrainingAudience.TEST_ACCOUNTS)
    test_faculty = make_user(user_type=UserType.FACULTY, department=world.dept, is_test_account=True)
    real_student = make_user(user_type=UserType.STUDENT, department=world.dept, supervisor=test_faculty)
    test_student = make_user(user_type=UserType.STUDENT, department=world.dept, supervisor=test_faculty, is_test_account=True)

    with mock.patch("iic_booking.training.selection.notify.send") as send:
        call = selection.open_call(
            world.oic, {"equipment_id": world.equipment.equipment_id, "seats": 2, "deadline": (timezone.now() + timedelta(days=3)).isoformat()}
        )
    recipients = send.call_args.args[1]
    assert test_faculty in recipients and world.faculty not in recipients and world.faculty2 not in recipients

    body = {"call_id": call.pk, "need_category": "EXPLORATORY", "justification": "Needs the instrument for thesis work."}
    resp = client_for(test_faculty).post(f"{API}/nominations/", {**body, "student_id": real_student.id}, format="json")
    assert resp.status_code == 403 and resp.data["code"] == "training_not_available"
    resp = client_for(test_faculty).post(f"{API}/nominations/", {**body, "student_id": test_student.id}, format="json")
    assert resp.status_code == 201, resp.data


@pytest.mark.django_db
def test_seeded_test_emails_count_as_test_accounts(world):
    set_audience(TrainingAudience.TEST_ACCOUNTS)
    seeded = make_user(user_type=UserType.FACULTY, department=world.dept, email="test.faculty@iic-booking.test")
    assert access.in_audience(seeded) and not access.in_audience(world.faculty)
    set_audience(TrainingAudience.EVERYONE)
    assert access.in_audience(world.faculty)


# ---------------------------------------------------------------------------
# training_config management command
# ---------------------------------------------------------------------------
@pytest.mark.django_db
def test_training_config_command(world, admin_only_db):
    with pytest.raises(CommandError):
        call_command("training_config", "--module", "on", stdout=StringIO())
    with pytest.raises(CommandError):
        call_command("training_config", "--enable", f"{world.equipment.code},NOPE1", "--confirm", "TRAINING", stdout=StringIO())
    assert not TrainingEquipmentSetting.objects.exists()

    out = StringIO()
    call_command(
        "training_config", "--module", "on", "--audience", "TEST_ACCOUNTS", "--enable", world.equipment.code,
        "--confirm", "TRAINING", "--find", "FE-SEM", stdout=out,
    )
    text = out.getvalue()
    assert "module_enabled=True" in text and "audience=TEST_ACCOUNTS" in text
    assert f"find {world.equipment.code}" in text and "training_enabled=True" in text
    assert access.module_enabled() and access.pilot_equipment_ids() == {world.equipment.equipment_id}

    out = StringIO()
    call_command("training_pilot_status", "--json", stdout=out)
    import json

    report = json.loads(out.getvalue())
    assert report["module_enabled"] is True and report["env_module_enabled"] is False
    assert report["audience"] == "TEST_ACCOUNTS" and report["effective_equipment_codes"] == [world.equipment.code]
