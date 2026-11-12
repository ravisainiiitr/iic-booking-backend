"""UPS health: power-cut tracking, the analysis checks, the AI summary with its fallback, officer notices and the API."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentManager
from iic_booking.power import analysis, advisor, health, notify, service
from iic_booking.power.models import UpsDevice, UpsHealthAssessment, UpsPowerEvent, UpsReading
from iic_booking.research_copilot.services.llm_gateway import FakeInferenceProvider, LLMResult
from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

HEALTHY = {"input_voltage": 230, "output_voltage": 230, "output_frequency": 50, "load_percent": 40,
           "battery_percent": 100, "runtime_minutes": 30, "battery_voltage": 54.0, "temperature_c": 25,
           "on_battery": False, "on_bypass": False, "low_battery": False, "overload": False, "fault": False,
           "replace_battery": False}


@pytest.fixture
def world():
    dept = Department.objects.create(name=f"UPS H {uuid.uuid4().hex[:5]}", code=f"H{uuid.uuid4().hex[:5]}")
    agent = DepartmentSyncAgent.objects.create(agent_name=f"A-{uuid.uuid4().hex[:5]}", department=dept,
                                               machine_guid=uuid.uuid4(), status=AgentLifecycleStatus.ENROLLED,
                                               is_active=True)
    eq = Equipment.objects.create(name="FE-SEM", code=f"F{uuid.uuid4().hex[:5]}", slot_duration_minutes=60,
                                  internal_department=dept, user_rating_enabled=False)
    ups = UpsDevice.objects.create(name="Vertiv GXT5 Lab 1", sync_agent=agent, department=dept, host="10.0.0.5",
                                   model="GXT5-10K", map_verified=True)
    ups.equipment.add(eq)
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True, email=f"oic{uuid.uuid4().hex[:5]}@x.in")
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return {"dept": dept, "agent": agent, "eq": eq, "ups": ups, "oic": oic}


def _fill(ups, *, days, now, every=timedelta(minutes=30), **override):
    """History readings every ``every`` for ``days`` days up to ``now``; values may be callables of age in days."""
    rows, t = [], now - timedelta(days=days)
    while t <= now:
        age = (now - t).total_seconds() / 86400
        values = {**HEALTHY, **{k: (v(age) if callable(v) else v) for k, v in override.items()}}
        rows.append(UpsReading(device=ups, read_at=t, reachable=True, **values))
        t += every
    UpsReading.objects.bulk_create(rows)


def _tick(ups, at, **values):
    """A fresh reading at ``at`` (later runs need live data, or the check reports 'no recent data')."""
    UpsReading.objects.create(device=ups, read_at=at, reachable=True, **{**HEALTHY, **values})


def _codes(a):
    return {f.code: f.level for f in a.findings}


def _send(agent, ups, at, **values):
    return service.record(agent, [{"device_id": ups.pk, "read_at": at.isoformat(), "reachable": True,
                                   "values": {**HEALTHY, **values}}])


# --- power events ---

def test_power_cut_is_tracked_from_start_to_end(world):
    now = timezone.now()
    with patch.object(notify, "_send"):
        _send(world["agent"], world["ups"], now - timedelta(minutes=10), on_battery=True, battery_percent=100,
              runtime_minutes=20)
        _send(world["agent"], world["ups"], now - timedelta(minutes=5), on_battery=True, battery_percent=80,
              runtime_minutes=15)
        _send(world["agent"], world["ups"], now, on_battery=False, battery_percent=82)
    e = UpsPowerEvent.objects.get(device=world["ups"])
    assert e.kind == "on_battery" and e.ended_at is not None
    assert (e.start_battery_percent, e.end_battery_percent) == (100, 80)
    assert round(e.minutes) == 10


# --- analysis ---

def test_healthy_ups_has_no_findings(world):
    now = timezone.now()
    _fill(world["ups"], days=20, now=now)
    a = analysis.assess(world["ups"], now=now)
    assert a.level == "ok" and a.findings == [] and a.risk_score == 0


def test_unverified_map_is_only_something_to_watch(world):
    world["ups"].map_verified = False
    world["ups"].save()
    now = timezone.now()
    _fill(world["ups"], days=2, now=now)
    a = analysis.assess(world["ups"], now=now)
    assert _codes(a) == {"map_unverified": "watch"} and a.risk_score == 0


def test_battery_capacity_fading_is_an_early_warning(world):
    now = timezone.now()
    # runtime at the same load falls from 30 to 21 min over 20 days (30% less)
    _fill(world["ups"], days=20, now=now, runtime_minutes=lambda age: 21 + 9 * min(1, age / 20))
    a = analysis.assess(world["ups"], now=now)
    assert _codes(a)["battery_capacity_falling"] == "attention"
    assert a.metrics["capacity_change_percent"] < -20


def test_battery_draining_faster_than_the_ups_predicts(world):
    now = timezone.now()
    _fill(world["ups"], days=2, now=now)
    # UPS estimated 20 min from 100 %, i.e. 5 %/min; it actually lost 60 % in 5 min (12 %/min)
    UpsPowerEvent.objects.create(device=world["ups"], kind="on_battery", started_at=now - timedelta(hours=3),
                                 ended_at=now - timedelta(hours=3) + timedelta(minutes=5),
                                 last_seen_at=now - timedelta(hours=3) + timedelta(minutes=5),
                                 start_battery_percent=100, end_battery_percent=40, start_runtime_minutes=20)
    a = analysis.assess(world["ups"], now=now)
    assert _codes(a)["fast_discharge"] == "attention"
    assert a.metrics["discharge_ratio"] == 2.4


def test_short_backup_heat_old_battery_mains_and_cuts(world):
    ups = world["ups"]
    ups.battery_installed_on = date.today() - timedelta(days=int(4.2 * 365))
    ups.save()
    now = timezone.now()
    _fill(ups, days=3, now=now, runtime_minutes=4, temperature_c=36,
          input_voltage=lambda age: 190 if int(age * 48) % 3 == 0 else 230)
    for i in range(3):
        start = now - timedelta(hours=2 + i * 5)
        UpsPowerEvent.objects.create(device=ups, kind="on_battery", started_at=start, ended_at=start + timedelta(minutes=2),
                                     last_seen_at=start + timedelta(minutes=2))
    codes = _codes(analysis.assess(ups, now=now))
    assert codes["short_backup"] == "urgent"  # 4 min against the 10 min the instruments need
    assert codes["too_hot"] == "urgent"
    assert codes["battery_old"] == "attention"  # warm room: expected life 3 years
    assert codes["unstable_mains"] == "attention"
    assert codes["frequent_power_cuts"] == "attention"


def test_load_growing_towards_the_limit(world):
    now = timezone.now()
    _fill(world["ups"], days=14, now=now, load_percent=lambda age: 85 - 2 * age)  # +2 points a day, 85 % now
    a = analysis.assess(world["ups"], now=now)
    assert _codes(a)["load_growing"] == "attention"


def test_unusual_value_against_its_own_week(world):
    now = timezone.now()
    _fill(world["ups"], days=7, now=now, every=timedelta(minutes=15),
          output_voltage=lambda age: 260 if age < 1 / 24 else 230 + (int(age * 96) % 5) * 0.5)
    assert _codes(analysis.assess(world["ups"], now=now))["unusual_output_voltage"] == "watch"


def test_current_on_battery_state_is_urgent(world):
    now = timezone.now()
    _fill(world["ups"], days=1, now=now - timedelta(minutes=1))
    UpsReading.objects.create(device=world["ups"], read_at=now, reachable=True, **{**HEALTHY, "on_battery": True})
    assert _codes(analysis.assess(world["ups"], now=now))["on_battery"] == "urgent"


# --- AI summary ---

def _attention(world):
    return analysis.Assessment("attention", 20, [analysis.Finding("warm", "attention", "UPS room warm", "About 31 °C.",
                                                                  "Improve cooling.")], {})


def test_summary_written_by_the_language_model(world):
    fake = FakeInferenceProvider(reply="The UPS for FE-SEM needs attention soon because its room is warm at about "
                                       "31 °C. Please improve the cooling around it.")
    text, source, model = advisor.summarise(world["ups"], _attention(world), ["FE-SEM"], gateway=fake)
    assert source == "ai" and model == "fake-test" and "warm" in text
    sent = fake.calls[0][1]["content"]
    assert "UPS room warm" in sent and "FE-SEM" in sent  # the model only gets the findings


@pytest.mark.parametrize("reply", ["", "As an AI I cannot help", "ok"])
def test_bad_model_reply_falls_back_to_the_rules_summary(world, reply):
    text, source, _ = advisor.summarise(world["ups"], _attention(world), ["FE-SEM"],
                                        gateway=FakeInferenceProvider(reply=reply))
    assert source == "rules" and text.startswith("Vertiv GXT5 Lab 1 (powers FE-SEM) needs attention soon.")


def test_model_down_falls_back_to_the_rules_summary(world):
    class Down(FakeInferenceProvider):
        def complete(self, *a, **k):
            raise OSError("connection refused")

    assert advisor.summarise(world["ups"], _attention(world), [], gateway=Down())[1] == "rules"


def test_deterministic_fallback_gateway_is_not_used_as_ai(world):
    class Local(FakeInferenceProvider):
        def complete(self, *a, **k):
            return LLMResult(text="I am IITR Equipment Booking Assistant. Ask about booking and more things here.",
                             model="fallback", provider="local")

    assert advisor.summarise(world["ups"], _attention(world), [], gateway=Local())[1] == "rules"


# --- officer notices ---

def test_power_cut_tells_the_officer_in_charge_at_once(world):
    with patch("iic_booking.communication.service.CommunicationService.send_email") as email, \
            patch("iic_booking.communication.in_app.notify_in_app") as bell:
        _send(world["agent"], world["ups"], timezone.now(), on_battery=True, runtime_minutes=12)
    assert email.call_count == 1
    kwargs = email.call_args.kwargs
    assert kwargs["recipient"] == world["oic"] and kwargs["template"] == "ups_power_alert_oic"
    assert "on battery" in kwargs["template_context"]["alert_title"]
    assert kwargs["template_context"]["equipment_names"] == "FE-SEM"
    assert bell.call_args.args[0] == [world["oic"]]


def test_unverified_or_switched_off_ups_tells_nobody(world, settings):
    world["ups"].map_verified = False
    world["ups"].save()
    with patch("iic_booking.communication.service.CommunicationService.send_email") as email:
        _send(world["agent"], world["ups"], timezone.now(), on_battery=True)
    assert email.call_count == 0
    world["ups"].map_verified = True
    world["ups"].save()
    settings.UPS_HEALTH_NOTIFY = False
    assert notify.enabled_for(world["ups"]) is False


def test_department_admin_is_told_when_no_officer_is_mapped(world):
    world["ups"].equipment.clear()
    admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=world["dept"], admin_approved=True)
    assert notify.recipients(world["ups"]) == [admin]


def test_health_notice_once_then_on_escalation_or_after_the_repeat_time(world):
    now = timezone.now()
    ups = world["ups"]
    _fill(ups, days=2, now=now, temperature_c=31)  # warm: attention
    with patch("iic_booking.communication.service.CommunicationService.send_email") as email, \
            patch("iic_booking.communication.in_app.notify_in_app"), \
            patch.object(advisor, "summarise", return_value=("Warm room.", "ai", "m")):
        first = health.run(ups, now=now)
        assert first.notified_codes == ["warm"] and first.notified_to == [world["oic"].pk]
        assert email.call_args.kwargs["template"] == "ups_health_attention_oic"
        assert "AI assistant" in email.call_args.kwargs["template_context"]["summary_note"]
        _tick(ups, now + timedelta(hours=1), temperature_c=31)
        again = health.run(ups, now=now + timedelta(hours=1))
        assert again.notified_at is None and email.call_count == 1  # same finding: not repeated within 7 days
        UpsReading.objects.filter(device=ups).update(temperature_c=37)  # now urgent: escalation is told at once
        _tick(ups, now + timedelta(hours=2), temperature_c=37)
        hot = health.run(ups, now=now + timedelta(hours=2))
        assert hot.notified_codes == ["too_hot"] and email.call_count == 2
        _tick(ups, now + timedelta(hours=15), temperature_c=37)
        later = health.run(ups, now=now + timedelta(hours=15))  # urgent repeats after 12 hours
        assert later.notified_codes == ["too_hot"] and email.call_count == 3


def test_same_findings_reuse_the_summary_instead_of_asking_the_model_again(world):
    now = timezone.now()
    _fill(world["ups"], days=1, now=now, temperature_c=31)
    with patch.object(advisor, "summarise", return_value=("Warm.", "ai", "m")) as ask, \
            patch.object(notify, "health_notice"):
        health.run(world["ups"], now=now)
        _tick(world["ups"], now + timedelta(hours=1), temperature_c=31)
        health.run(world["ups"], now=now + timedelta(hours=1))
    assert ask.call_count == 1
    assert UpsHealthAssessment.objects.filter(device=world["ups"]).count() == 2


# --- API ---

def test_health_api_for_the_officer_and_not_for_others(world):
    now = timezone.now()
    _fill(world["ups"], days=1, now=now, temperature_c=31)
    with patch.object(notify, "health_notice"), patch.object(advisor, "summarise", return_value=("Warm.", "ai", "m")):
        health.run(world["ups"], now=now)
    c = APIClient()
    c.force_authenticate(world["oic"])
    body = c.get(f"/api/power/ups/{world['ups'].pk}/health/").json()
    assert body["latest"]["level"] == "attention" and body["latest"]["summary"] == "Warm."
    assert body["officers"] and body["notify_officers"] is True
    listing = c.get("/api/power/ups/").json()["results"][0]
    assert listing["health"]["level"] == "attention"
    stranger = APIClient()
    stranger.force_authenticate(UserFactory(user_type=UserType.STUDENT, admin_approved=True))
    assert stranger.get(f"/api/power/ups/{world['ups'].pk}/health/").status_code == 404


def test_run_now_is_throttled(world):
    c = APIClient()
    c.force_authenticate(world["oic"])
    with patch.object(notify, "health_notice"):
        first = c.post(f"/api/power/ups/{world['ups'].pk}/health/run/").json()
        second = c.post(f"/api/power/ups/{world['ups'].pk}/health/run/").json()
    assert first["id"] == second["id"]
