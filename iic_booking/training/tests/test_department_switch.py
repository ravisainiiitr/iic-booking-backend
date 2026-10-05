"""Per-department Training switch (department_modules) on top of the global switch, audience and equipment scope."""

from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from iic_booking.department_modules import services
from iic_booking.department_modules.constants import ModuleKey
from iic_booking.equipment.models import EquipmentManager
from iic_booking.training import access
from iic_booking.training.models import DemoRequest
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, make_user
from .test_module_admin import demo_payload

pytestmark = pytest.mark.django_db


def set_training(world, department, **kwargs):
    return services.set_module(
        world.admin, department, ModuleKey.TRAINING, reason="Department training rollout", **kwargs
    )


def boot(user):
    return client_for(user).get(f"{API}/bootstrap/").data


def test_department_off_removes_its_equipment_and_hides_training_from_its_users(world):
    assert access.pilot_equipment_ids() is None  # env switch on, no list: every equipment in scope
    resp = client_for(world.faculty2).post(f"{API}/demo-requests/", demo_payload(world.other_equipment), format="json")
    assert resp.status_code == 201, resp.data

    set_training(world, world.other_dept, enabled=False)

    scope = access.pilot_equipment_ids()
    assert world.equipment.equipment_id in scope and world.other_equipment.equipment_id not in scope
    assert boot(world.faculty2)["enabled"] is False
    assert boot(world.student2)["enabled"] is False
    assert boot(world.faculty)["enabled"] is True
    assert boot(world.other_oic)["menus"]["training_workspace"] is False
    assert boot(world.oic)["menus"]["training_workspace"] is True
    resp = client_for(world.faculty).post(f"{API}/demo-requests/", demo_payload(world.other_equipment), format="json")
    assert resp.status_code == 400 and resp.data["code"] == "not_in_pilot"
    # Existing records stay intact.
    assert DemoRequest.objects.filter(equipment=world.other_equipment).count() == 1

    set_training(world, world.other_dept, enabled=True)
    assert access.pilot_equipment_ids() is None
    assert boot(world.faculty2)["enabled"] is True


def test_department_off_also_narrows_an_explicit_equipment_list(world):
    from iic_booking.training import module_config

    module_config.set_equipment_enabled(world.admin, world.equipment, True)
    module_config.set_equipment_enabled(world.admin, world.other_equipment, True)
    set_training(world, world.dept, enabled=False)
    assert access.pilot_equipment_ids() == {world.other_equipment.equipment_id}


def test_staff_keep_roles_through_the_equipment_department(world):
    oic = make_user(user_type=UserType.MANAGER, department=world.other_dept)
    EquipmentManager.objects.create(equipment=world.equipment, manager=oic)
    set_training(world, world.other_dept, enabled=False)
    assert boot(oic)["menus"]["training_workspace"] is True


def test_test_users_only_department(world):
    test_faculty = make_user(user_type=UserType.FACULTY, department=world.dept, is_test_account=True)
    set_training(world, world.dept, test_users_only=True)

    assert boot(world.faculty)["enabled"] is False
    assert boot(world.student)["enabled"] is False
    assert boot(test_faculty)["enabled"] is True
    assert boot(world.faculty2)["enabled"] is True  # other department unaffected

    # On the test-only department's equipment only test accounts may ask for a demonstration.
    resp = client_for(world.faculty2).post(f"{API}/demo-requests/", demo_payload(world.equipment), format="json")
    assert resp.status_code == 403 and resp.data["code"] == access.AUDIENCE_CODE
    assert client_for(world.faculty2).post(
        f"{API}/demo-requests/", demo_payload(world.other_equipment), format="json"
    ).status_code == 201
    assert client_for(test_faculty).post(f"{API}/demo-requests/", demo_payload(world.equipment), format="json").status_code == 201

    from iic_booking.training import selection

    with mock.patch("iic_booking.training.selection.notify.send") as send:
        call = selection.open_call(
            world.oic,
            {"equipment_id": world.equipment.equipment_id, "seats": 2,
             "deadline": (timezone.now() + timedelta(days=3)).isoformat()},
        )
    # A call on the test-only department's equipment only reaches test accounts.
    recipients = send.call_args.args[1]
    assert test_faculty in recipients and world.faculty2 not in recipients and world.faculty not in recipients

    body = {"call_id": call.pk, "need_category": "EXPLORATORY", "justification": "Needs the instrument for thesis work."}
    resp = client_for(world.faculty2).post(f"{API}/nominations/", {**body, "student_id": world.student2.id}, format="json")
    assert resp.status_code == 403 and resp.data["code"] == access.AUDIENCE_CODE

    # A call on an unrestricted department's equipment reaches everyone except the test-only department's other users.
    with mock.patch("iic_booking.training.selection.notify.send") as send:
        selection.open_call(
            world.other_oic,
            {"equipment_id": world.other_equipment.equipment_id, "seats": 2,
             "deadline": (timezone.now() + timedelta(days=3)).isoformat()},
        )
    recipients = send.call_args.args[1]
    assert test_faculty in recipients and world.faculty2 in recipients and world.faculty not in recipients
