"""Demo request state machine, curtailment (final + notified), charges/refunds, scheduling and SLA."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, SlotStatus
from iic_booking.training import demo, notify
from iic_booking.training.errors import TrainingError
from iic_booking.training.models import DemoRequestRevision, DemoStatus, TrainingPolicy
from iic_booking.users.models.wallet import SubWallet

from .conftest import API, at, client_for, fund_faculty, future_day, make_slots


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def record(code, recipients, **kwargs):
        calls.append({"code": code, "recipients": [getattr(r, "id", r) for r in recipients], **kwargs})

    monkeypatch.setattr(notify, "send", record)
    return calls


def _payload(world, day, **extra):
    data = {
        "equipment_id": world.equipment.equipment_id,
        "purpose": "COURSE",
        "course_code": "CY-501",
        "course_name": "Advanced Characterisation",
        "participants_requested": 20,
        "requested_duration_minutes": 120,
        "preferred_windows": [{"start": at(day, 10).isoformat(), "end": at(day, 12).isoformat()}],
    }
    data.update(extra)
    return data


def _set_rate(rate):
    TrainingPolicy.objects.filter(scope="GLOBAL").update(demo_rate_per_hour=Decimal(rate))


@pytest.mark.django_db
def test_course_demo_full_flow_free_and_slots_labelled(world, sent):
    _set_rate("600")
    day = future_day()
    make_slots(world.equipment, day)
    resp = client_for(world.faculty).post(f"{API}/demo-requests/", _payload(world, day), format="json")
    assert resp.status_code == 201, resp.data
    rid = resp.data["id"]
    assert resp.data["status"] == "SUBMITTED"
    assert any(c["code"] == "demo_request_submitted_oic_email" and world.oic.id in c["recipients"] for c in sent)

    detail = client_for(world.oic).get(f"{API}/demo-requests/{rid}/")
    assert detail.data["status"] == "UNDER_REVIEW"
    assert detail.data["permissions"]["decide"] is True

    resp = client_for(world.oic).post(
        f"{API}/demo-requests/{rid}/decide/", {"action": "approve", "start_at": at(day, 10).isoformat()}, format="json"
    )
    assert resp.status_code == 200, resp.data
    assert resp.data["status"] == "SCHEDULED"
    assert resp.data["charge_mode"] == "FREE" and resp.data["charge_amount"] == "0.00"
    labels = set(DailySlot.objects.filter(status=SlotStatus.BLOCKED).values_list("blocked_label", flat=True))
    assert labels == {"Demo: CY-501 Advanced Characterisation (Prof. Asha Rao)"}
    actions = list(DemoRequestRevision.objects.filter(request_id=rid).values_list("action", flat=True))
    assert actions == ["submitted", "viewed", "approved", "scheduled"]

    resp = client_for(world.operator).post(f"{API}/demo-requests/{rid}/complete/", {"attended_count": 18}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["status"] == "COMPLETED" and resp.data["attended_count"] == 18


@pytest.mark.django_db
def test_curtailment_requires_reason_is_final_and_notifies_faculty(world, sent):
    day = future_day()
    req = demo.create_request(world.faculty, _payload(world, day))
    with pytest.raises(TrainingError) as exc:
        demo.decide(req, world.oic, {"action": "approve", "approved_duration_minutes": 60})
    assert exc.value.code == "reason_required"
    with pytest.raises(TrainingError):
        demo.decide(req, world.oic, {"action": "approve", "approved_participants": 25, "reason_code": "OTHER"})

    req = demo.decide(req, world.oic, {"action": "approve", "approved_duration_minutes": 60, "approved_participants": 10, "reason_code": "SAFETY_CAPACITY", "remarks": "Room fits 10"})
    assert req.status == DemoStatus.APPROVED and req.curtailed
    assert req.curtail_reason_code == "SAFETY_CAPACITY"
    rev = req.revisions.get(action="approved_curtailed")
    assert rev.reason_code == "SAFETY_CAPACITY" and rev.after["approved_duration_minutes"] == 60
    note = next(c for c in sent if c["code"] == "demo_request_decision_faculty_email")
    assert note["recipients"] == [world.faculty.id]
    assert "final" in note["message"]
    # No acceptance step: the faculty member cannot respond to a curtailed approval.
    with pytest.raises(TrainingError):
        demo.respond(req, world.faculty, response="decline")


@pytest.mark.django_db
def test_propose_counter_once_then_accept_schedules(world, sent):
    day = future_day()
    make_slots(world.equipment, day)
    req = demo.create_request(world.faculty, _payload(world, day))
    req = demo.decide(req, world.oic, {"action": "propose", "start_at": at(day, 13).isoformat()})
    assert req.status == DemoStatus.PROPOSED_ALTERNATIVE and req.proposal_expires_at > timezone.now()

    counter = [{"start": at(day, 14).isoformat(), "end": at(day, 16).isoformat()}]
    req = demo.respond(req, world.faculty, response="counter", windows=counter)
    assert req.status == DemoStatus.UNDER_REVIEW and req.counter_used

    req = demo.decide(req, world.oic, {"action": "propose", "start_at": at(day, 14).isoformat()})
    with pytest.raises(TrainingError):
        demo.respond(req, world.faculty, response="counter", windows=counter)
    req = demo.respond(req, world.faculty, response="accept")
    assert req.status == DemoStatus.SCHEDULED
    assert req.approved_start_at == at(day, 14)


@pytest.mark.django_db
def test_proposal_expires_after_working_days(world, sent):
    day = future_day(20)
    req = demo.create_request(world.faculty, _payload(world, day))
    req = demo.decide(req, world.oic, {"action": "propose", "start_at": at(day, 13).isoformat()})
    assert demo.expire_proposals(now=timezone.now() + timedelta(days=2)) == 0
    assert demo.expire_proposals(now=timezone.now() + timedelta(days=10)) == 1
    req.refresh_from_db()
    assert req.status == DemoStatus.EXPIRED


@pytest.mark.django_db
def test_research_demo_charge_debit_and_refunds(world, sent):
    _set_rate("600")
    sub = fund_faculty(world.faculty, world.dept, Decimal("1000.00"))
    day = future_day(10)
    data = _payload(world, day, purpose="RESEARCH_INDUCTION", requested_duration_minutes=90)
    with pytest.raises(TrainingError) as exc:
        demo.create_request(world.faculty, data)
    assert exc.value.code == "charge_ack_required"
    req = demo.create_request(world.faculty, {**data, "charge_acknowledged": True})

    req = demo.decide(req, world.oic, {"action": "approve"})
    assert req.charge_mode == "WALLET" and req.charge_amount == Decimal("900.00")
    sub.refresh_from_db()
    assert sub.balance == Decimal("100.00")

    req = demo.cancel(req, world.oic, "Instrument down")
    assert req.status == DemoStatus.CANCELLED and req.cancelled_by_side == "IIC"
    assert req.refund_amount == Decimal("900.00")
    sub.refresh_from_db()
    assert sub.balance == Decimal("1000.00")


@pytest.mark.django_db
def test_faculty_cancellation_refund_windows(world, sent):
    _set_rate("600")
    sub = fund_faculty(world.faculty, world.dept, Decimal("2000.00"))
    day = future_day(3)
    make_slots(world.equipment, day)
    req = demo.create_request(
        world.faculty, _payload(world, day, purpose="OTHER", requested_duration_minutes=60, charge_acknowledged=True)
    )
    req = demo.decide(req, world.oic, {"action": "approve", "start_at": at(day, 10).isoformat()})
    assert req.status == DemoStatus.SCHEDULED
    req = demo.cancel(req, world.faculty, "Course rescheduled")
    assert req.cancelled_by_side == "FACULTY"
    assert req.refund_amount == Decimal("300.00")  # 50%: between 2 and 7 days before
    sub.refresh_from_db()
    assert sub.balance == Decimal("1700.00")
    assert not DailySlot.objects.filter(status=SlotStatus.BLOCKED).exists()


@pytest.mark.django_db
def test_insufficient_balance_blocks_approval_and_oic_can_waive(world, sent):
    _set_rate("600")
    fund_faculty(world.faculty, world.dept, Decimal("50.00"))
    req = demo.create_request(world.faculty, _payload(world, future_day(), purpose="OTHER", charge_acknowledged=True))
    with pytest.raises(TrainingError) as exc:
        demo.decide(req, world.oic, {"action": "approve"})
    assert exc.value.code == "insufficient_balance"
    req.refresh_from_db()
    assert req.status in (DemoStatus.SUBMITTED, DemoStatus.UNDER_REVIEW)
    req = demo.decide(req, world.oic, {"action": "approve", "charge_mode": "FREE"})
    assert req.status == DemoStatus.APPROVED and req.charge_amount == 0


@pytest.mark.django_db
def test_course_demo_never_charged_even_if_oic_sets_rate(world, sent):
    _set_rate("600")
    req = demo.create_request(world.faculty, _payload(world, future_day()))
    req = demo.decide(req, world.oic, {"action": "approve", "charge_mode": "WALLET", "rate_per_hour": "900"})
    assert req.charge_mode == "FREE" and req.charge_amount == 0 and not req.wallet_txn_id
    assert not SubWallet.objects.exists()


@pytest.mark.django_db
def test_schedule_refuses_booked_window_and_keeps_approved(world, sent):
    day = future_day()
    slots = make_slots(world.equipment, day)
    DailySlot.objects.filter(pk=slots[1].pk).update(status=SlotStatus.BOOKED)
    req = demo.decide(demo.create_request(world.faculty, _payload(world, day)), world.oic, {"action": "approve"})
    resp = client_for(world.oic).post(f"{API}/demo-requests/{req.pk}/schedule/", {"start_at": at(day, 9).isoformat()}, format="json")
    assert resp.status_code == 400 and resp.data["code"] == "slots_not_free"
    assert resp.data["conflicts"][0]["slot_id"] == slots[1].pk
    req.refresh_from_db()
    assert req.status == DemoStatus.APPROVED
    assert DailySlot.objects.get(pk=slots[1].pk).status == SlotStatus.BOOKED
    assert DailySlot.objects.get(pk=slots[0].pk).status == SlotStatus.AVAILABLE


@pytest.mark.django_db
def test_withdraw_and_sla_escalation(world, sent):
    req = demo.create_request(world.faculty, _payload(world, future_day()))
    assert demo.escalate_overdue(now=timezone.now() + timedelta(days=1)) == 0
    assert demo.escalate_overdue(now=timezone.now() + timedelta(days=10)) == 1
    req.refresh_from_db()
    assert req.sla_escalated_at is not None
    assert any(c["code"] == "demo_request_escalated_email" and world.admin.id in c["recipients"] for c in sent)
    req = demo.withdraw(req, world.faculty)
    assert req.status == DemoStatus.WITHDRAWN


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("who", "expected"),
    [("oic", 200), ("temp_oic", 200), ("admin", 200), ("other_oic", 403), ("operator", 403), ("faculty2", 403)],
)
def test_decide_permission_matrix(world, sent, who, expected):
    req = demo.create_request(world.faculty, _payload(world, future_day()))
    resp = client_for(getattr(world, who)).post(f"{API}/demo-requests/{req.pk}/decide/", {"action": "reject", "remarks": "No"}, format="json")
    assert resp.status_code == expected, resp.data
