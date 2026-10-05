"""Per-department DSA switch: booking feed and new workspaces for agents; existing work keeps flowing."""

import uuid

import pytest

from iic_booking.department_modules.constants import ModuleKey
from iic_booking.sync.exceptions import DepartmentModuleDisabledError
from iic_booking.sync.models import (
    AgentAssignment,
    AgentLifecycleStatus,
    BookingWorkspace,
    DepartmentSyncAgent,
    EquipmentSyncProfile,
)
from iic_booking.sync.services.dataplane import BookingSyncService, EquipmentSyncService, WorkspaceService

from .conftest import DAY, make_booking, switch

pytestmark = pytest.mark.django_db
DSA = ModuleKey.DSA


def agent_for(*equipment):
    agent = DepartmentSyncAgent.objects.create(
        agent_name=f"Agent {uuid.uuid4().hex[:4]}",
        department=equipment[0].internal_department,
        machine_guid=uuid.uuid4(),
        status=AgentLifecycleStatus.ENROLLED,
        is_active=True,
    )
    for eq in equipment:
        profile = EquipmentSyncProfile.objects.create(equipment=eq)
        AgentAssignment.objects.create(sync_agent=agent, sync_profile=profile, is_active=True)
    return agent


def feed(agent):
    return {b["booking_id"] for b in BookingSyncService().list_for_agent(agent)["results"]}


def test_department_off_stops_new_work_and_keeps_existing(world):
    agent = agent_for(world.equipment, world.other_equipment)
    earlier = make_booking(world.student, world.equipment, created_ago=DAY)
    switch(world.admin, world.dept, DSA, enabled=False)
    later = make_booking(world.student, world.equipment)
    other = make_booking(world.student, world.other_equipment)

    assert feed(agent) == {earlier.booking_id, other.booking_id}
    assert WorkspaceService().create_or_get(agent, booking_id=earlier.booking_id)["created"] is True
    with pytest.raises(DepartmentModuleDisabledError) as exc:
        WorkspaceService().create_or_get(agent, booking_id=later.booking_id)
    assert exc.value.status_code == 403 and exc.value.code == "DSA_DEPARTMENT_DISABLED"
    assert not BookingWorkspace.objects.filter(booking=later).exists()

    # Agent configuration keeps working so results of existing bookings can still be uploaded.
    assert EquipmentSyncService().list_for_agent(agent)["count"] == 2

    switch(world.admin, world.dept, DSA, enabled=True)
    assert later.booking_id in feed(agent)


def test_existing_workspace_is_always_returned(world):
    agent = agent_for(world.equipment)
    switch(world.admin, world.dept, DSA, enabled=False)
    booking = make_booking(world.student, world.equipment)
    BookingWorkspace.objects.create(
        sync_agent=agent, booking=booking, equipment=world.equipment, workspace_name="W", relative_folder="r",
        expected_result_folder="r/Results",
    )
    assert booking.booking_id in feed(agent)
    assert WorkspaceService().create_or_get(agent, booking_id=booking.booking_id)["created"] is False


def test_test_users_only(world):
    agent = agent_for(world.equipment)
    earlier = make_booking(world.student, world.equipment, created_ago=DAY)
    switch(world.admin, world.dept, DSA, test_users_only=True)
    real = make_booking(world.student, world.equipment)
    test = make_booking(world.test_student, world.equipment)
    assert feed(agent) == {earlier.booking_id, test.booking_id}
    assert WorkspaceService().create_or_get(agent, booking_id=test.booking_id)["created"] is True
    with pytest.raises(DepartmentModuleDisabledError):
        WorkspaceService().create_or_get(agent, booking_id=real.booking_id)


def test_new_equipment_pc_assignment_hides_off_departments(world):
    from iic_booking.device_provisioning.services import list_unassigned_equipment

    ids = {r["equipment_id"] for r in list_unassigned_equipment(department_id=None)}
    assert {world.equipment.pk, world.other_equipment.pk} <= ids
    switch(world.admin, world.dept, DSA, enabled=False)
    ids = {r["equipment_id"] for r in list_unassigned_equipment(department_id=None)}
    assert world.equipment.pk not in ids and world.other_equipment.pk in ids
