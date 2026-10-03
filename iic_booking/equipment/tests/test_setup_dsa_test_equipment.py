from __future__ import annotations

import uuid
from io import StringIO

import pytest
from django.core.management import CommandError, call_command

from iic_booking.equipment.models import ChargeProfile, DailySlot, Equipment, EquipmentManager, EquipmentOperator
from iic_booking.sync.models import AgentAssignment, DepartmentSyncAgent
from iic_booking.users.models import Department, DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.test_accounts import user_email_for_type
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def seeded():
    dept = Department.objects.create(name="IIC", code="IIC", department_type=DepartmentType.INTERNAL)
    users = {}
    for user_type in (UserType.STUDENT, UserType.FACULTY, UserType.MANAGER, UserType.OPERATOR):
        users[user_type] = UserFactory(
            email=user_email_for_type(user_type), user_type=user_type, is_test_account=True, admin_approved=True
        )
    return dept, users


def _run(*args) -> str:
    out = StringIO()
    call_command("setup_dsa_test_equipment", *args, stdout=out)
    return out.getvalue()


def test_requires_confirm(seeded):
    with pytest.raises(CommandError):
        _run()
    assert not Equipment.objects.filter(code="DSATEST").exists()


def test_status_is_read_only(seeded):
    out = _run("--status")
    assert "found: false" in out
    assert not Equipment.objects.filter(code="DSATEST").exists()


def test_creates_hidden_zero_charge_equipment_idempotently(seeded):
    dept, users = seeded
    _run("--confirm", "SETUP_DSA_TEST", "--slot-days", "3")
    _run("--confirm", "SETUP_DSA_TEST", "--slot-days", "3")

    eq = Equipment.objects.get(code="DSATEST")
    assert eq.visible_to_test_accounts_only is True
    assert eq.internal_department_id == dept.pk
    assert eq.input_fields.filter(field_key="A").count() == 2
    profiles = ChargeProfile.objects.filter(equipment=eq, is_active=True)
    assert profiles.count() == 4
    assert all(p.primary_unit_charge == 0 and p.secondary_unit_charge == 0 for p in profiles)
    assert list(EquipmentManager.objects.filter(equipment=eq).values_list("manager_id", flat=True)) == [
        users[UserType.MANAGER].pk
    ]
    assert list(EquipmentOperator.objects.filter(equipment=eq).values_list("operator_id", flat=True)) == [
        users[UserType.OPERATOR].pk
    ]
    assert DailySlot.objects.filter(slot_master__equipment=eq).count() == 3 * 8


def test_missing_test_accounts_are_never_created(seeded):
    _, users = seeded
    users[UserType.OPERATOR].delete()
    with pytest.raises(CommandError, match="operator"):
        _run("--confirm", "SETUP_DSA_TEST", "--slot-days", "0")
    assert not Equipment.objects.filter(code="DSATEST").exists()


def test_assigns_only_the_named_agent(seeded):
    dept, _ = seeded
    agent = DepartmentSyncAgent.objects.create(
        agent_uuid=uuid.uuid4(), agent_name="DSA RAVI", machine_name="RAVI", machine_guid=uuid.uuid4(), department=dept
    )
    DepartmentSyncAgent.objects.filter(pk=agent.pk).update(bootstrap_required=False)
    _run("--confirm", "SETUP_DSA_TEST", "--slot-days", "0", "--dsa-machine-name", "ravi")
    eq = Equipment.objects.get(code="DSATEST")
    active = AgentAssignment.objects.filter(sync_profile__equipment=eq, is_active=True)
    assert [a.sync_agent_id for a in active] == [agent.id]
    agent.refresh_from_db()
    assert agent.equipment_id is None
    assert agent.bootstrap_required is True
