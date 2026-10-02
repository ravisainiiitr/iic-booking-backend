"""OIC waiver of the demonstration charge, and the training-department list behind the request form."""

from decimal import Decimal

import pytest

from iic_booking.training import demo, module_config, notify
from iic_booking.training.errors import TrainingError
from iic_booking.training.models import DemoStatus, TrainingAuditLog
from iic_booking.users.models.wallet import SubWalletTransaction

from .conftest import (
    API,
    at,
    client_for,
    fund_faculty,
    future_day,
    internal_rate_profile,
    make_department,
    make_equipment,
    set_course_demos_free,
)

REASON = "Joint outreach session agreed with the department"


@pytest.fixture(autouse=True)
def charged_course_demos(db):
    set_course_demos_free(False)


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def record(code, recipients, **kwargs):
        calls.append({"code": code, "recipients": [getattr(r, "id", r) for r in recipients], **kwargs})

    monkeypatch.setattr(notify, "send", record)
    return calls


def _request(world, day=None, minutes=120):
    day = day or future_day(12)
    return demo.create_request(
        world.faculty,
        {
            "equipment_id": world.equipment.equipment_id,
            "purpose": "COURSE",
            "course_code": "CY-501",
            "participants_requested": 10,
            "requested_duration_minutes": minutes,
            "preferred_windows": [{"start": at(day, 10).isoformat(), "end": at(day, 12).isoformat()}],
            "charge_acknowledged": True,
        },
    )


@pytest.fixture
def funded(world):
    internal_rate_profile(world.equipment, "600.00")
    return fund_faculty(world.faculty, world.dept, Decimal("5000.00"))


@pytest.mark.django_db
@pytest.mark.parametrize("reason", ["", "   ", "too short"])
def test_waiver_requires_a_reason_of_ten_characters(world, sent, funded, reason):
    req = _request(world)
    with pytest.raises(TrainingError) as exc:
        demo.decide(req, world.oic, {"action": "approve", "waive_charge": True, "waiver_reason": reason})
    assert exc.value.code == "waiver_reason_required"
    req.refresh_from_db()
    assert req.status == DemoStatus.SUBMITTED and not req.wallet_txn_id


@pytest.mark.django_db
def test_approve_with_waiver_debits_nothing_and_is_audited(world, sent, funded):
    req = _request(world)
    resp = client_for(world.oic).post(
        f"{API}/demo-requests/{req.pk}/decide/",
        {"action": "approve", "waive_charge": True, "waiver_reason": REASON},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    funded.refresh_from_db()
    assert funded.balance == Decimal("5000.00")
    assert resp.data["charge_mode"] == "WAIVED" and resp.data["charged"] is False
    assert resp.data["charge_amount"] == "1200.00"
    assert resp.data["charge_text"].startswith(f"Charge waived by {demo._faculty_name(world.oic)} — {REASON}")
    assert resp.data["charge_waiver"]["reason"] == REASON and resp.data["charge_waiver"]["by_id"] == world.oic.id
    assert resp.data["permissions"]["waive"] is False

    log = TrainingAuditLog.objects.get(action="demo.charge_waived", object_id=str(req.pk))
    assert log.actor_id == world.oic.id and log.note == REASON and log.created_at
    assert log.after["amount_waived"] == "1200.00" and log.after["refunded"] == "0.00"

    note = next(c for c in sent if c["code"] == "demo_request_decision_faculty_email")
    assert f"waived by {demo._faculty_name(world.oic)} — {REASON}" in note["message"] and "₹1,200.00" in note["message"]
    assert note["context"]["charge"].startswith("Charge waived by")

    faculty_view = client_for(world.faculty).get(f"{API}/demo-requests/{req.pk}/").data
    assert faculty_view["charge_text"].startswith(f"Charge waived by {demo._faculty_name(world.oic)} — {REASON}")


@pytest.mark.django_db
def test_proposal_with_waiver_is_not_debited_on_accept(world, sent, funded):
    day = future_day(12)
    req = _request(world, day, minutes=60)
    req = demo.decide(
        req, world.oic, {"action": "propose", "start_at": at(day, 14).isoformat(), "waive_charge": "true", "waiver_reason": REASON}
    )
    req = demo.respond(req, world.faculty, response="accept")
    funded.refresh_from_db()
    assert req.charge_mode == "WAIVED" and not req.wallet_txn_id and funded.balance == Decimal("5000.00")


@pytest.mark.django_db
def test_waiver_after_debit_refunds_in_full(world, sent, funded):
    day = future_day(12)
    req = demo.decide(_request(world, day, minutes=60), world.oic, {"action": "approve"})
    funded.refresh_from_db()
    assert funded.balance == Decimal("4400.00")

    oic = client_for(world.oic)
    assert oic.get(f"{API}/demo-requests/{req.pk}/").data["permissions"]["waive"] is True
    assert oic.post(f"{API}/demo-requests/{req.pk}/waive/", {"reason": "short"}, format="json").status_code == 400
    resp = oic.post(f"{API}/demo-requests/{req.pk}/waive/", {"reason": REASON}, format="json")
    assert resp.status_code == 200, resp.data
    funded.refresh_from_db()
    assert funded.balance == Decimal("5000.00")
    req.refresh_from_db()
    refund = SubWalletTransaction.objects.get(pk=req.refund_txn_id)
    assert refund.amount == Decimal("600.00")
    assert refund.description == (
        f"Demonstration charge waived – full refund – {world.equipment.code} – {day.strftime('%d %b %Y')} ({req.reference})"
    )
    assert "₹600.00 refunded" in resp.data["charge_text"]
    log = TrainingAuditLog.objects.get(action="demo.charge_waived", object_id=str(req.pk))
    assert log.before == {"charge_mode": "WALLET"} and log.after["refunded"] == "600.00"
    note = next(c for c in sent if c.get("event") == "training.demo.charge_waived")
    assert "₹600.00 was refunded" in note["message"]

    again = oic.post(f"{API}/demo-requests/{req.pk}/waive/", {"reason": REASON}, format="json")
    assert again.status_code == 400 and again.data["code"] == "nothing_to_waive"
    demo.cancel(req, world.faculty)
    funded.refresh_from_db()
    assert funded.balance == Decimal("5000.00")  # no second refund


@pytest.mark.django_db
def test_operator_and_other_oic_cannot_waive(world, sent, funded):
    req = _request(world)
    for user in (world.operator, world.other_oic):
        resp = client_for(user).post(
            f"{API}/demo-requests/{req.pk}/decide/",
            {"action": "approve", "waive_charge": True, "waiver_reason": REASON},
            format="json",
        )
        assert resp.status_code == 403
    req = demo.decide(req, world.oic, {"action": "approve"})
    operator = client_for(world.operator)
    assert operator.get(f"{API}/demo-requests/{req.pk}/").data["permissions"]["waive"] is False
    assert operator.post(f"{API}/demo-requests/{req.pk}/waive/", {"reason": REASON}, format="json").status_code == 403
    assert client_for(world.faculty).post(f"{API}/demo-requests/{req.pk}/waive/", {"reason": REASON}, format="json").status_code == 403
    req.refresh_from_db()
    assert req.charge_mode == "WALLET" and not TrainingAuditLog.objects.filter(action="demo.charge_waived").exists()


@pytest.mark.django_db
def test_main_admin_can_waive(world, sent, funded):
    req = demo.decide(_request(world), world.admin, {"action": "approve", "waive_charge": True, "waiver_reason": REASON})
    assert req.charge_mode == "WAIVED" and not req.wallet_txn_id


@pytest.mark.django_db
def test_waiver_without_internal_rate_needs_no_rate(world, sent):
    sub = fund_faculty(world.faculty, world.dept, Decimal("5000.00"))
    req = demo.decide(_request(world, minutes=60), world.oic, {"action": "approve", "waive_charge": True, "waiver_reason": REASON})
    sub.refresh_from_db()
    assert req.charge_mode == "WAIVED" and req.charge_amount == 0 and sub.balance == Decimal("5000.00")


@pytest.mark.django_db
def test_waiver_ignored_when_course_demos_are_free(world, sent, funded):
    set_course_demos_free(True)
    req = demo.decide(_request(world), world.oic, {"action": "approve", "waive_charge": True, "waiver_reason": REASON})
    assert req.charge_mode == "FREE"
    assert not TrainingAuditLog.objects.filter(action="demo.charge_waived").exists()


@pytest.mark.django_db
def test_department_list_follows_training_scope(world, training_settings):
    third_dept = make_department(name="Not In Training Dept")
    make_equipment(third_dept, name="Raman")
    faculty = client_for(world.faculty)

    module_config.set_equipment_enabled(world.admin, world.equipment, True)
    names = {d["name"] for d in faculty.get(f"{API}/equipment/").data["departments"]}
    assert names == {world.dept.name}

    training_settings.TRAINING_PILOT_EQUIPMENT_CODES = world.other_equipment.code
    data = faculty.get(f"{API}/equipment/").data
    assert {d["id"] for d in data["departments"]} == {world.dept.pk, world.other_dept.pk}
    assert third_dept.pk not in {d["id"] for d in data["departments"]}
    assert all(d["equipment_count"] == 1 for d in data["departments"])
