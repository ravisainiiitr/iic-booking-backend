"""Sessions, attendance → TRAINED award + badge, module flag gating, pilot scope, policy versioning,
pending actions, housekeeping and email catalog."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, EquipmentOperator, SlotStatus
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.training import notify
from iic_booking.training.models import (
    AwardStatus,
    CertificationAward,
    EventKind,
    EventStatus,
    Registration,
    RegistrationStatus,
    TrainingEvent,
    TrainingPolicy,
    UserBadge,
)
from iic_booking.training.tasks import housekeeping
from iic_booking.users.models.user_type import UserType

from .conftest import API, at, client_for, future_day, make_slots, make_user


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(notify, "send", lambda *a, **k: None)


def _event_with_participants(world, *students):
    event = TrainingEvent.objects.create(
        kind=EventKind.HANDS_ON, title="FE-SEM hands-on", slug=f"ev-{timezone.now().timestamp()}", equipment=world.equipment,
        status=EventStatus.SELECTION_PUBLISHED,
    )
    for st in students:
        Registration.objects.create(event=event, user=st, status=RegistrationStatus.CONFIRMED)
    return event


@pytest.mark.django_db
def test_sessions_reserve_and_attendance_issues_trained_award_and_badge(world):
    day = future_day()
    make_slots(world.equipment, day)
    a = make_user(user_type=UserType.STUDENT, department=world.dept, name="A")
    b = make_user(user_type=UserType.STUDENT, department=world.dept, name="B")
    event = _event_with_participants(world, a, b)
    oic = client_for(world.oic)
    r1 = oic.post(f"{API}/events/{event.pk}/sessions/", {"start_at": at(day, 9).isoformat(), "end_at": at(day, 11).isoformat()}, format="json")
    assert r1.status_code == 201, r1.data
    assert r1.data["status"] == "SCHEDULED" and r1.data["slots_reserved"]
    assert set(DailySlot.objects.filter(status=SlotStatus.BLOCKED).values_list("blocked_label", flat=True)) == {"Training: FE-SEM hands-on"}
    r2 = oic.post(f"{API}/events/{event.pk}/sessions/", {"start_at": at(day, 13).isoformat(), "end_at": at(day, 14).isoformat(), "reserve_slots": False}, format="json")
    assert r2.data["status"] == "PLANNED"

    from iic_booking.training.models import TrainingSession

    TrainingSession.objects.filter(event=event).update(start_at=timezone.now() - timedelta(hours=3), end_at=timezone.now() - timedelta(hours=2))
    roster = client_for(world.operator).get(f"{API}/sessions/{r1.data['id']}/attendance/").data["roster"]
    rows = [{"registration_id": r["registration_id"], "status": "PRESENT"} for r in roster]
    assert client_for(world.other_oic).post(f"{API}/sessions/{r1.data['id']}/attendance/", {"rows": rows}, format="json").status_code == 403
    resp = client_for(world.operator).post(f"{API}/sessions/{r1.data['id']}/attendance/", {"rows": rows}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["awarded_user_ids"] == []  # second session still open

    rows2 = [{"registration_id": r["registration_id"], "status": "PRESENT" if r["user_id"] == a.id else "ABSENT"} for r in roster]
    resp = client_for(world.operator).post(f"{API}/sessions/{r2.data['id']}/attendance/", {"rows": rows2}, format="json")
    assert resp.data["awarded_user_ids"] == [a.id]
    award = CertificationAward.objects.get(user=a)
    assert award.level.code == "TRAINED" and award.status == AwardStatus.ACTIVE
    assert 715 <= (award.valid_until - award.awarded_at).days <= 735
    assert UserBadge.objects.filter(user=a, badge__code="trained", equipment=world.equipment, award=award).exists()
    assert Registration.objects.get(event=event, user=b).status == RegistrationStatus.PARTIAL
    event.refresh_from_db()
    assert event.status == EventStatus.COMPLETED

    badges = client_for(world.faculty).get(f"{API}/badges/?user_ids={a.id}").data["results"]
    assert badges == {}  # a is not in the faculty's group
    badges = client_for(world.oic).get(f"{API}/badges/?user_ids={a.id}").data["results"]
    assert badges[str(a.id)][0]["equipment_code"] == world.equipment.code
    mine = client_for(a).get(f"{API}/me/trainings/").data
    assert mine["certifications"][0]["level"] == "TRAINED" and mine["badges"]


@pytest.mark.django_db
def test_operator_coverage_scope_for_attendance(world):
    other_operator = make_user(user_type=UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=world.other_equipment, operator=other_operator, role=EquipmentOperator.Role.PRIMARY)
    event = _event_with_participants(world, world.student)
    from iic_booking.training.models import TrainingSession

    s = TrainingSession.objects.create(event=event, equipment=world.equipment, start_at=timezone.now() - timedelta(hours=2), end_at=timezone.now() - timedelta(hours=1))
    assert client_for(other_operator).get(f"{API}/sessions/{s.pk}/attendance/").status_code == 403
    assert client_for(world.operator).get(f"{API}/sessions/{s.pk}/attendance/").status_code == 200
    assert client_for(world.operator).get(f"{API}/attendance/sessions/").data["results"][0]["needs_attendance"]


@pytest.mark.django_db
def test_module_off_hides_everything_except_bootstrap_and_admin_policy(world, settings):
    settings.TRAINING_MODULE_ENABLED = False
    boot = client_for(world.faculty).get(f"{API}/bootstrap/")
    assert boot.status_code == 200 and boot.data["enabled"] is False
    assert not any(boot.data["menus"][k] for k in ("training_events", "my_trainings", "training_workspace", "training_attendance"))
    resp = client_for(world.faculty).get(f"{API}/demo-requests/")
    assert resp.status_code == 403 and resp.data["code"] == "training_disabled"
    assert client_for(world.oic).get(f"{API}/calls/").status_code == 403
    assert client_for(world.admin).get(f"{API}/policy/").status_code == 200
    assert client_for(world.faculty).get(f"{API}/badges/").data == {"results": {}}
    assert housekeeping() == {"skipped": "disabled"}
    keys = {i["key"] for i in collect_pending_actions(world.oic)}
    assert not any(k.startswith("training_") for k in keys)


@pytest.mark.django_db
def test_bootstrap_menus_by_role(world):
    def menus(user):
        return client_for(user).get(f"{API}/bootstrap/").data["menus"]

    assert menus(world.faculty)["training_events"] and not menus(world.faculty)["training_workspace"]
    assert menus(world.student)["my_trainings"]
    assert menus(world.oic)["training_workspace"] and menus(world.temp_oic)["training_workspace"]
    assert menus(world.operator)["training_attendance"] and not menus(world.operator)["training_workspace"]
    assert menus(world.admin)["training_policy_settings"]


@pytest.mark.django_db
def test_pilot_equipment_and_oic_allow_list(world, settings):
    settings.TRAINING_PILOT_EQUIPMENT_CODES = world.other_equipment.code
    resp = client_for(world.faculty).post(
        f"{API}/demo-requests/",
        {"equipment_id": world.equipment.equipment_id, "purpose": "COURSE", "course_code": "X", "participants_requested": 2,
         "requested_duration_minutes": 60, "preferred_windows": [{"start": at(future_day(), 10).isoformat(), "end": at(future_day(), 11).isoformat()}]},
        format="json",
    )
    assert resp.status_code == 400 and resp.data["code"] == "not_in_pilot"
    assert client_for(world.oic).get(f"{API}/bootstrap/").data["menus"]["training_workspace"] is False
    settings.TRAINING_PILOT_EQUIPMENT_CODES = ""
    settings.TRAINING_PILOT_OIC_EMAILS = world.other_oic.email
    assert client_for(world.oic).get(f"{API}/bootstrap/").data["menus"]["training_workspace"] is False
    assert client_for(world.other_oic).get(f"{API}/bootstrap/").data["menus"]["training_workspace"] is True


@pytest.mark.django_db
def test_policy_publish_creates_new_version(world):
    admin = client_for(world.admin)
    v1 = TrainingPolicy.objects.get(scope="GLOBAL", is_active=True)
    resp = admin.post(f"{API}/policy/", {"scope": "GLOBAL", "reserved_pct": 25, "scoring_weights": {"first_time_equipment": 28, "bogus": 1}}, format="json")
    assert resp.status_code == 201, resp.data
    assert resp.data["version"] == v1.version + 1 and resp.data["reserved_pct"] == 25
    assert resp.data["scoring_weights"] == {"first_time_equipment": 28.0}
    v1.refresh_from_db()
    assert v1.is_active is False
    assert admin.post(f"{API}/policy/", {"scope": "GLOBAL", "reserved_pct": 95}, format="json").status_code == 400
    assert client_for(world.oic).get(f"{API}/policy/").status_code == 403
    resp = admin.post(f"{API}/policy/", {"scope": "EQUIPMENT", "equipment_id": world.equipment.equipment_id, "demo_rate_per_hour": "750"}, format="json")
    assert resp.status_code == 201 and resp.data["reserved_pct"] == 25 and resp.data["demo_rate_per_hour"] == "750.00"
    assert len(admin.get(f"{API}/policy/history/").data["results"]) == 3


@pytest.mark.django_db
def test_pending_actions_include_training_items(world):
    from iic_booking.training import demo

    demo.create_request(
        world.faculty,
        {"equipment_id": world.equipment.equipment_id, "purpose": "COURSE", "course_code": "CY-1", "participants_requested": 5,
         "requested_duration_minutes": 60, "preferred_windows": [{"start": at(future_day(), 10).isoformat(), "end": at(future_day(), 11).isoformat()}]},
    )
    assert "training_demo_requests" in {i["key"] for i in collect_pending_actions(world.oic)}
    assert "training_demo_requests" in {i["key"] for i in collect_pending_actions(world.temp_oic)}
    assert "training_demo_requests" not in {i["key"] for i in collect_pending_actions(world.other_oic)}
    assert "training_demo_requests" not in {i["key"] for i in collect_pending_actions(world.operator)}


@pytest.mark.django_db
def test_housekeeping_runs_when_enabled(world):
    out = housekeeping()
    assert set(out) == {"proposals_expired", "seats_expired", "demo_escalated"}


def test_email_catalog_contains_training_templates():
    from iic_booking.communication.default_email_templates import (
        DEFAULT_EMAIL_TEMPLATE_CODES,
        get_default_email_template,
        get_default_email_templates,
    )

    codes = {
        "demo_request_submitted_oic_email",
        "demo_request_decision_faculty_email",
        "training_selection_result_email",
        "certification_awarded_email",
    }
    assert codes <= set(DEFAULT_EMAIL_TEMPLATE_CODES)
    all_codes = {t["code"] for t in get_default_email_templates()}
    assert codes <= all_codes
    tpl = get_default_email_template("training_selection_result_email")
    assert "{{ summary }}" in tpl["body_html"] or "summary" in tpl["body_text"]


@pytest.mark.django_db
def test_notify_creates_missing_template_without_overwriting():
    from iic_booking.communication.models import CommunicationTemplate

    t = notify.ensure_template("certification_awarded_email")
    assert t is not None and t.is_active
    CommunicationTemplate.objects.filter(pk=t.pk).update(subject="Custom subject")
    again = notify.ensure_template("certification_awarded_email")
    assert again.pk == t.pk and again.subject == "Custom subject"
