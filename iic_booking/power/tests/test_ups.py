"""UPS monitoring: agent config and readings, alerts that open once and resolve, history, silence, staff visibility."""

from __future__ import annotations

import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentManager
from iic_booking.power import service
from iic_booking.power.models import UpsDevice, UpsReading
from iic_booking.power.register_maps import validate_points
from iic_booking.sync.authentication import DepartmentSyncAgentAuthentication, SyncAgentUser
from iic_booking.sync.models import AgentLifecycleStatus, AlertEvent, DepartmentSyncAgent
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _agent(dept):
    return DepartmentSyncAgent.objects.create(agent_name=f"A-{uuid.uuid4().hex[:5]}", department=dept, machine_guid=uuid.uuid4(),
                                              status=AgentLifecycleStatus.ENROLLED, is_active=True)


@pytest.fixture
def world():
    dept = Department.objects.create(name=f"UPS Dept {uuid.uuid4().hex[:5]}", code=f"U{uuid.uuid4().hex[:5]}")
    agent, other_agent = _agent(dept), _agent(dept)
    eq = Equipment.objects.create(name="FE-SEM", code=f"F{uuid.uuid4().hex[:5]}", slot_duration_minutes=60,
                                  internal_department=dept, user_rating_enabled=False)
    ups = UpsDevice.objects.create(name="Vertiv GXT5 Lab 1", sync_agent=agent, department=dept, host="10.0.0.5", model="GXT5-10K")
    ups.equipment.add(eq)
    other = UpsDevice.objects.create(name="Other", sync_agent=other_agent, department=dept, host="10.0.0.6")
    return {"dept": dept, "agent": agent, "eq": eq, "ups": ups, "other": other}


def as_agent(agent):
    def fake(self, request):
        request.sync_agent = agent
        return SyncAgentUser(agent), None
    return patch.object(DepartmentSyncAgentAuthentication, "authenticate", fake)


def reading(device, **values):
    return {"device_id": device.pk, "read_at": timezone.now().isoformat(), "reachable": True,
            "values": {"input_voltage": 230.1, "output_voltage": 229.8, "load_percent": 35, "battery_percent": 100,
                       "runtime_minutes": 42, "on_battery": False, **values}}


def open_alerts(device):
    return set(AlertEvent.objects.filter(category="ups", details__ups_id=device.pk, status__in=("NEW", "ACKNOWLEDGED"))
               .values_list("rule_code", flat=True))


def test_agent_gets_only_its_own_devices_with_points(world):
    with as_agent(world["agent"]):
        data = APIClient().get("/api/v1/sync/ups/devices/").json()
    assert [d["device_id"] for d in data["devices"]] == [world["ups"].pk]
    names = {p["name"] for p in data["devices"][0]["points"]}
    assert {"battery_percent", "on_battery", "load_percent"} <= names


def test_readings_are_stored_and_foreign_devices_ignored(world):
    with as_agent(world["agent"]):
        res = APIClient().post("/api/v1/sync/ups/readings/", {"readings": [reading(world["ups"]), reading(world["other"])]},
                               format="json").json()
    assert res == {"stored": 1, "ignored": 1}
    r = UpsReading.objects.get(device=world["ups"])
    assert (r.input_voltage, r.load_percent, r.on_battery) == (230.1, 35.0, False)
    assert not UpsReading.objects.filter(device=world["other"]).exists()


def test_power_cut_raises_alerts_once_and_they_resolve(world):
    ups = world["ups"]
    service.record(world["agent"], [reading(ups, on_battery=True, battery_percent=25)])
    assert open_alerts(ups) == {"ups_on_battery", "ups_low_battery"}
    service.record(world["agent"], [reading(ups, on_battery=True, battery_percent=20)])
    assert AlertEvent.objects.filter(rule_code="ups_on_battery").count() == 1  # not duplicated
    alert = AlertEvent.objects.get(rule_code="ups_on_battery")
    assert alert.severity == "CRITICAL" and alert.equipment == world["eq"] and alert.sync_agent == world["agent"]
    service.record(world["agent"], [reading(ups)])  # mains back, battery full
    assert open_alerts(ups) == set()
    assert AlertEvent.objects.get(rule_code="ups_on_battery").status == "RESOLVED"


def test_unreachable_overload_fault_and_battery_replacement(world):
    ups = world["ups"]
    service.record(world["agent"], [{"device_id": ups.pk, "reachable": False, "error": "timeout", "values": {}}])
    assert open_alerts(ups) == {"ups_unreachable"}
    service.record(world["agent"], [reading(ups, load_percent=97, fault=True, replace_battery=True)])
    assert open_alerts(ups) == {"ups_overload", "ups_fault", "ups_replace_battery"}


def test_history_keeps_the_latest_and_one_point_every_five_minutes(world):
    ups = world["ups"]
    start = timezone.now() - timedelta(minutes=20)
    for i in range(0, 20 * 60 + 1, 30):  # a reading every 30 s for 20 minutes
        item = reading(ups)
        item["read_at"] = (start + timedelta(seconds=i)).isoformat()
        service.record(world["agent"], [item])
    times = list(UpsReading.objects.filter(device=ups).order_by("read_at").values_list("read_at", flat=True))
    assert 4 <= len(times) <= 6  # one every 5 minutes plus the latest, not 41
    assert times[-1] == start + timedelta(seconds=20 * 60)


def test_silence_is_flagged_and_cleared(world):
    ups = world["ups"]
    service.check_silent()
    assert "ups_silent" in open_alerts(ups)
    service.record(world["agent"], [reading(ups)])
    service.check_silent()
    assert "ups_silent" not in open_alerts(ups)


def test_staff_see_only_their_ups(world):
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=world["eq"], manager=oic)
    student = UserFactory(user_type=UserType.STUDENT, admin_approved=True)
    service.record(world["agent"], [reading(world["ups"], on_battery=True)])

    def names(user):
        c = APIClient()
        c.force_authenticate(user)
        return [d["name"] for d in c.get("/api/power/ups/").json()["results"]]

    assert set(names(admin)) == {"Vertiv GXT5 Lab 1", "Other"}
    assert names(oic) == ["Vertiv GXT5 Lab 1"]
    assert names(student) == []
    c = APIClient()
    c.force_authenticate(oic)
    row = c.get("/api/power/ups/").json()["results"][0]
    assert row["latest"]["on_battery"] is True and row["map_verified"] is False
    assert any(a["rule"] == "ups_on_battery" for a in row["latest"]["alerts"])
    assert c.get(f"/api/power/ups/{world['ups'].pk}/history/").status_code == 200
    assert c.get(f"/api/power/ups/{world['other'].pk}/history/").status_code == 404


def test_custom_map_validation():
    assert validate_points([]) == ["The map must be a non-empty list of points."]
    assert validate_points([{"name": "battery_percent", "table": "input", "address": 7, "type": "uint16"}]) == []
    problems = validate_points([{"name": "x", "table": "ram", "address": -1, "type": "text"}])
    assert len(problems) == 3
