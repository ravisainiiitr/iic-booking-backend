"""Equipment upkeep: maintenance schedule, uptime metrics from disruption events, early-warning alerts, access."""

import uuid
from datetime import timedelta
from unittest import mock

import pytest
from django.utils import timezone

from iic_booking.equipment.disruption_models import DisruptionEvent, DisruptionScope, DisruptionEventSlot, DisruptionType
from iic_booking.training.tests.conftest import client_for, world  # noqa: F401
from iic_booking.upkeep import service
from iic_booking.upkeep.models import AlertKind, MaintenancePlan, PlanCompletion, ReliabilityAlert, UpkeepSetting

pytestmark = pytest.mark.django_db
API = "/api/v1/upkeep"


@pytest.fixture(autouse=True)
def _quiet():
    with mock.patch("iic_booking.communication.in_app.notify_in_app") as m:
        yield m


def outage(eq, kind, start_days_ago, hours, *, scope=DisruptionScope.EQUIPMENT, ended=True):
    start = timezone.now() - timedelta(days=start_days_ago)
    return DisruptionEvent.objects.create(
        equipment=eq, disruption_type=kind, scope=scope, start_at=start, end_at=start + timedelta(hours=hours),
        ended_at=start + timedelta(hours=hours) if ended else None,
    )


def open_alerts(eq):
    return set(ReliabilityAlert.objects.filter(equipment=eq, resolved_at__isnull=True).values_list("kind", flat=True))


# --------------------------------------------------------------------------- metrics
def test_uptime_merges_overlaps_and_separates_planned_and_staff_time(world):
    eq = world.equipment
    outage(eq, DisruptionType.UNDER_MAINTENANCE, 20, 10)
    dup = timezone.now() - timedelta(days=20) + timedelta(hours=5)  # same outage recorded again, 5 h overlap
    DisruptionEvent.objects.create(equipment=eq, disruption_type=DisruptionType.UNDER_MAINTENANCE, scope=DisruptionScope.EQUIPMENT,
                                   start_at=dup, ended_at=dup + timedelta(hours=10))
    outage(eq, DisruptionType.UNDER_MAINTENANCE, 5, 2)
    outage(eq, DisruptionType.SCHEDULED_MAINTENANCE, 10, 6)
    outage(eq, DisruptionType.OPERATOR_ABSENT, 3, 3)
    m = service.metrics(eq, 30)
    assert m["breakdowns"] == 2 and m["breakdown_hours"] == pytest.approx(17.0, abs=0.1)
    assert m["planned_hours"] == pytest.approx(6.0, abs=0.1)
    assert m["downtime_hours"] == pytest.approx(23.0, abs=0.1)
    assert m["staff_unavailable_hours"] == pytest.approx(3.0, abs=0.1)
    window_h = 30 * 24
    assert m["uptime_pct"] == pytest.approx(100 * (window_h - 23) / window_h, abs=0.01)
    assert m["mttr_hours"] == pytest.approx(8.5, abs=0.1)
    assert m["mtbf_hours"] == pytest.approx((window_h - 23) / 2, abs=0.2)
    assert sum(c["count"] for c in m["causes"]) == 4  # staff absence is not an equipment cause


def test_slot_disruption_counts_only_its_slots(world):
    eq = world.equipment
    start = timezone.now() - timedelta(days=4)
    ev = DisruptionEvent.objects.create(equipment=eq, disruption_type=DisruptionType.UNDER_MAINTENANCE, scope=DisruptionScope.SLOTS,
                                        start_at=start, end_at=start + timedelta(days=2))
    DisruptionEventSlot.objects.create(event=ev, start_datetime=start, end_datetime=start + timedelta(hours=1))
    DisruptionEventSlot.objects.create(event=ev, start_datetime=start + timedelta(days=2) - timedelta(hours=1),
                                      end_datetime=start + timedelta(days=2))
    m = service.metrics(eq, 30)
    assert m["breakdown_hours"] == pytest.approx(2.0, abs=0.01) and m["breakdowns"] == 2


def test_open_whole_equipment_outage_runs_until_now(world):
    outage(world.equipment, DisruptionType.UNDER_MAINTENANCE, 2, 1, ended=False)
    assert service.metrics(world.equipment, 30)["breakdown_hours"] == pytest.approx(48.0, abs=0.1)


# --------------------------------------------------------------------------- access
def test_access_staff_only_and_oic_manages(world):
    eq = world.equipment
    assert client_for(world.student).get(f"{API}/equipment/").data["results"] == []
    assert client_for(world.student).get(f"{API}/equipment/{eq.pk}/").status_code == 403
    assert client_for(world.other_oic).get(f"{API}/equipment/{eq.pk}/").status_code == 403
    listed = client_for(world.operator).get(f"{API}/equipment/").data["results"]
    assert [r["equipment"]["equipment_id"] for r in listed] == [eq.pk] and listed[0]["can_manage"] is False
    body = {"title": "Vacuum pump oil change", "kind": "PREVENTIVE", "interval_days": 90}
    assert client_for(world.operator).post(f"{API}/equipment/{eq.pk}/plans/", body, format="json").status_code == 403
    assert client_for(world.other_oic).post(f"{API}/equipment/{eq.pk}/plans/", body, format="json").status_code == 403
    resp = client_for(world.oic).post(f"{API}/equipment/{eq.pk}/plans/", body, format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["next_due_on"] == (timezone.localdate() + timedelta(days=90)).isoformat() and resp.data["state"] == "OK"
    assert client_for(world.admin).get(f"{API}/equipment/{eq.pk}/").data["can_manage"] is True


def test_settings_validation(world):
    url = f"{API}/equipment/{world.equipment.pk}/"
    assert client_for(world.operator).put(url, {"uptime_target_pct": 90}, format="json").status_code == 403
    assert client_for(world.oic).put(url, {"uptime_target_pct": 101}, format="json").status_code == 400
    assert client_for(world.oic).put(url, {"breakdown_window_days": 2}, format="json").status_code == 400
    resp = client_for(world.oic).put(url, {"uptime_target_pct": "97.5", "breakdown_alert_count": 2}, format="json")
    assert resp.status_code == 200 and resp.data["setting"]["uptime_target_pct"] == 97.5
    assert resp.data["setting"]["breakdown_alert_count"] == 2 and resp.data["metrics"]["target_pct"] == 97.5


# --------------------------------------------------------------------------- schedule and alerts
def test_overdue_plan_alerts_then_completion_advances_and_resolves(world, _quiet, django_capture_on_commit_callbacks):
    eq = world.equipment
    today = timezone.localdate()
    resp = client_for(world.oic).post(f"{API}/equipment/{eq.pk}/plans/", {
        "title": "XRD calibration with Si standard", "kind": "CALIBRATION", "interval_days": 180,
        "next_due_on": (today - timedelta(days=3)).isoformat(), "responsible_id": world.operator.pk,
    }, format="json")
    assert resp.status_code == 201 and resp.data["state"] == "OVERDUE"
    plan_id = resp.data["id"]
    with django_capture_on_commit_callbacks(execute=True):
        assert service.scan()["opened"] == 1
    assert open_alerts(eq) == {AlertKind.PLAN_OVERDUE}
    _quiet.assert_called()
    assert service.scan()["opened"] == 0  # raised once while open

    url = f"{API}/plans/{plan_id}/complete/"
    future = (today + timedelta(days=1)).isoformat()
    assert client_for(world.operator).post(url, {"done_on": future}, format="json").status_code == 400
    assert client_for(world.student).post(url, {}, format="json").status_code == 403
    resp = client_for(world.operator).post(url, {"notes": "Within tolerance", "report_reference": "CAL/2026/11"}, format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["plan"]["next_due_on"] == (today + timedelta(days=180)).isoformat() and resp.data["plan"]["state"] == "OK"
    assert PlanCompletion.objects.filter(plan_id=plan_id).count() == 1
    assert open_alerts(eq) == set()


def test_due_soon_alert_and_closing_a_plan(world):
    eq = world.equipment
    plan = service.create_plan(eq, world.oic, {"title": "Chiller filter", "interval_days": 30, "lead_days": 7,
                                                "next_due_on": (timezone.localdate() + timedelta(days=5)).isoformat()})
    service.scan()
    assert open_alerts(eq) == {AlertKind.PLAN_DUE}
    url = f"{API}/plans/{plan.pk}/close/"
    assert client_for(world.oic).post(url, {}, format="json").status_code == 400
    assert client_for(world.oic).post(url, {"reason": "Chiller replaced"}, format="json").status_code == 200
    assert open_alerts(eq) == set()
    service.scan()
    assert open_alerts(eq) == set()
    assert client_for(world.oic).patch(f"{API}/plans/{plan.pk}/", {"interval_days": 10}, format="json").status_code == 400


def test_uptime_and_repeated_breakdown_warnings_open_and_resolve(world):
    eq = world.equipment
    UpkeepSetting.objects.create(equipment=eq, uptime_target_pct=99, breakdown_alert_count=2, breakdown_window_days=30)
    outage(eq, DisruptionType.UNDER_MAINTENANCE, 20, 24)
    outage(eq, DisruptionType.UNDER_MAINTENANCE, 5, 12)
    first = service.scan()
    assert open_alerts(eq) == {AlertKind.UPTIME_BELOW_TARGET, AlertKind.REPEATED_BREAKDOWNS} and first["opened"] == 2
    alert = ReliabilityAlert.objects.get(equipment=eq, kind=AlertKind.REPEATED_BREAKDOWNS)
    resp = client_for(world.operator).post(f"{API}/alerts/{alert.pk}/acknowledge/", {"note": "Engineer booked"}, format="json")
    assert resp.status_code == 200 and resp.data["acknowledged_by"]["id"] == world.operator.pk
    assert client_for(world.other_oic).post(f"{API}/alerts/{alert.pk}/acknowledge/", {}, format="json").status_code == 403
    assert {a["kind"] for a in client_for(world.oic).get(f"{API}/alerts/").data["results"]} == open_alerts(eq)
    assert client_for(world.other_oic).get(f"{API}/alerts/").data["results"] == []
    DisruptionEvent.objects.filter(equipment=eq).delete()
    assert service.scan()["resolved"] == 2 and open_alerts(eq) == set()


def test_ups_attention_raises_alert_for_its_equipment(world):
    from iic_booking.power.models import UpsDevice, UpsHealthAssessment
    from iic_booking.sync.models import DepartmentSyncAgent

    agent = DepartmentSyncAgent.objects.create(department=world.dept, agent_name="Lab agent", equipment=world.equipment, machine_guid=uuid.uuid4())
    dev = UpsDevice.objects.create(name="UPS-1", sync_agent=agent, host="10.0.0.5")
    dev.equipment.add(world.equipment)
    UpsHealthAssessment.objects.create(device=dev, level="urgent", summary="Battery capacity low.")
    service.scan()
    alert = ReliabilityAlert.objects.get(equipment=world.equipment, kind=AlertKind.UPS_HEALTH)
    assert alert.severity == "CRITICAL" and "Battery capacity low" in alert.message


def test_list_reports_uptime_alerts_and_next_due(world):
    eq = world.equipment
    outage(eq, DisruptionType.UNDER_MAINTENANCE, 10, 72)
    MaintenancePlan.objects.create(equipment=eq, title="PM", interval_days=30, next_due_on=timezone.localdate() + timedelta(days=40))
    service.scan()
    row = client_for(world.oic).get(f"{API}/equipment/", {"days": 90}).data["results"][0]
    assert row["uptime_pct"] == pytest.approx(100 * (90 * 24 - 72) / (90 * 24), abs=0.01)
    assert row["below_target"] is False and row["plans"] == 1 and row["can_manage"] is True
    assert row["next_due_on"] == (timezone.localdate() + timedelta(days=40)).isoformat()
    assert client_for(world.oic).get(f"{API}/equipment/", {"days": 0}).status_code == 400
