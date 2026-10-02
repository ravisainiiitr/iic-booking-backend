"""Demonstrations charged at the internal IITR rate, deducted from the faculty wallet, refunded per policy."""

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.training import charges, demo, notify
from iic_booking.training.errors import TrainingError
from iic_booking.training.models import DemoStatus, TrainingAuditLog, TrainingModuleSettings, TrainingPolicy
from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction
from iic_booking.users.models.user_type import UserType

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


def _payload(world, day=None, **extra):
    day = day or future_day()
    data = {
        "equipment_id": world.equipment.equipment_id,
        "purpose": "COURSE",
        "course_code": "CY-501",
        "participants_requested": 20,
        "requested_duration_minutes": 120,
        "preferred_windows": [{"start": at(day, 10).isoformat(), "end": at(day, 12).isoformat()}],
        "charge_acknowledged": True,
    }
    data.update(extra)
    return data


@pytest.mark.django_db
def test_shipped_default_charges_course_demonstrations():
    assert TrainingModuleSettings._meta.get_field("course_demos_free").default is False
    TrainingModuleSettings.objects.all().delete()
    assert charges.course_demos_free() is False
    assert charges.is_chargeable("COURSE") and charges.is_chargeable("OTHER")


@pytest.mark.django_db
def test_charge_equals_internal_hourly_rate_times_duration(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    internal_rate_profile(world.equipment, "1800.00", user_type=UserType.EXTERNAL)
    fund_faculty(world.faculty, world.dept, Decimal("5000.00"))
    req = demo.create_request(world.faculty, _payload(world, requested_duration_minutes=150))
    assert req.charge_mode == "WALLET" and req.rate_per_hour == Decimal("600.00")
    assert req.charge_amount == Decimal("1500.00")  # 2.5 h × ₹600, not the external rate
    req = demo.decide(req, world.oic, {"action": "approve", "rate_per_hour": "1"})
    assert req.charge_amount == Decimal("1500.00") and req.rate_per_hour == Decimal("600.00")


@pytest.mark.django_db
def test_sample_profile_rate_is_converted_to_instrument_time(world):
    internal_rate_profile(world.equipment, "500.00", profile_type="SAMPLE", time_formula="A * 30")
    rate = charges.internal_rate(world.equipment, world.faculty)
    assert rate.rate_per_hour == Decimal("1000.00")  # ₹500 per 30-minute sample
    assert charges.amount_for(rate.rate_per_hour, 90) == Decimal("1500.00")


@pytest.mark.django_db
def test_user_type_without_own_profile_falls_back_to_standard_faculty_rate(world):
    internal_rate_profile(world.equipment, "700.00")
    rate = charges.internal_rate(world.equipment, world.student)
    assert rate.rate_per_hour == Decimal("700.00")


@pytest.mark.django_db
def test_course_free_toggle(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    fund_faculty(world.faculty, world.dept, Decimal("5000.00"))
    charged = demo.create_request(world.faculty, _payload(world))
    assert charged.charge_mode == "WALLET" and charged.charge_amount == Decimal("1200.00")
    with pytest.raises(TrainingError) as exc:
        demo.create_request(world.faculty, _payload(world, charge_acknowledged=False))
    assert exc.value.code == "charge_ack_required"

    resp = client_for(world.admin).post(f"{API}/admin/module/", {"course_demos_free": True}, format="json")
    assert resp.status_code == 200 and resp.data["course_demos_free"] is True
    assert TrainingAuditLog.objects.filter(action="module.settings_updated", after__course_demos_free=True).exists()
    assert client_for(world.oic).post(f"{API}/admin/module/", {"course_demos_free": False}, format="json").status_code == 403

    free = demo.create_request(world.faculty, _payload(world, charge_acknowledged=False))
    assert free.charge_mode == "FREE" and free.charge_amount == 0
    free = demo.decide(free, world.oic, {"action": "approve"})
    assert free.charge_amount == 0 and not free.wallet_txn_id
    other = demo.create_request(world.faculty, _payload(world, purpose="OTHER"))
    assert other.charge_mode == "WALLET"
    quote = client_for(world.faculty).get(
        f"{API}/equipment/{world.equipment.equipment_id}/demo-quote/", {"purpose": "COURSE", "minutes": 60}
    )
    assert quote.status_code == 200 and quote.data["chargeable"] is False and quote.data["course_demos_free"] is True


@pytest.mark.django_db
def test_wallet_debit_on_approval_with_clear_description(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    sub = fund_faculty(world.faculty, world.dept, Decimal("2000.00"))
    day = future_day(12)
    req = demo.create_request(world.faculty, _payload(world, day, requested_duration_minutes=60))
    sub.refresh_from_db()
    assert sub.balance == Decimal("2000.00")  # nothing is taken at submission
    req = demo.decide(req, world.oic, {"action": "approve"})
    sub.refresh_from_db()
    assert sub.balance == Decimal("1400.00")
    txn = SubWalletTransaction.objects.get(pk=req.wallet_txn_id)
    assert txn.sub_wallet_id == sub.pk and txn.related_user_id == world.faculty.id
    assert txn.description == f"Demonstration charge – {world.equipment.code} – {day.strftime('%d %b %Y')} ({req.reference})"
    note = next(c for c in sent if c["code"] == "demo_request_decision_faculty_email")
    assert "₹600.00" in note["message"] and "Chemistry sub-wallet" in note["message"]
    assert "₹600.00" in note["context"]["charge"]


@pytest.mark.django_db
def test_accepting_a_proposed_time_debits(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    sub = fund_faculty(world.faculty, world.dept, Decimal("2000.00"))
    day = future_day(12)
    req = demo.create_request(world.faculty, _payload(world, day, requested_duration_minutes=60))
    req = demo.decide(req, world.oic, {"action": "propose", "start_at": at(day, 14).isoformat()})
    assert not req.wallet_txn_id
    assert "will be deducted" in next(c for c in sent if c["code"] == "demo_request_decision_faculty_email")["message"]
    req = demo.respond(req, world.faculty, response="accept")
    sub.refresh_from_db()
    assert req.wallet_txn_id and sub.balance == Decimal("1400.00")


@pytest.mark.django_db
@pytest.mark.parametrize(("days_ahead", "expected"), [(8, "600.00"), (7, "600.00"), (3, "300.00"), (2, "300.00"), (1, "0")])
def test_refund_tiers_on_faculty_cancellation(world, sent, days_ahead, expected):
    internal_rate_profile(world.equipment, "600.00")
    fund_faculty(world.faculty, world.dept, Decimal("2000.00"))
    req = demo.decide(demo.create_request(world.faculty, _payload(world, requested_duration_minutes=60)), world.oic, {"action": "approve"})
    now = timezone.now()
    req.approved_start_at = now + timedelta(days=days_ahead, minutes=1)
    assert demo.refund_amount_for(req, "FACULTY", now=now) == Decimal(expected)
    assert demo.refund_amount_for(req, "IIC", now=now) == Decimal("600.00")


@pytest.mark.django_db
def test_refund_tiers_follow_policy(world, sent):
    TrainingPolicy.objects.filter(scope="GLOBAL").update(demo_refund_full_days=10, demo_refund_half_days=5)
    internal_rate_profile(world.equipment, "600.00")
    fund_faculty(world.faculty, world.dept, Decimal("2000.00"))
    req = demo.decide(demo.create_request(world.faculty, _payload(world, requested_duration_minutes=60)), world.oic, {"action": "approve"})
    now = timezone.now()
    req.approved_start_at = now + timedelta(days=8)
    assert demo.refund_amount_for(req, "FACULTY", now=now) == Decimal("300.00")
    req.approved_start_at = now + timedelta(days=4)
    assert demo.refund_amount_for(req, "FACULTY", now=now) == Decimal("0")


@pytest.mark.django_db
def test_cancellation_refund_is_credited_with_description(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    sub = fund_faculty(world.faculty, world.dept, Decimal("2000.00"))
    req = demo.decide(demo.create_request(world.faculty, _payload(world, future_day(20), requested_duration_minutes=60)), world.oic, {"action": "approve"})
    req = demo.cancel(req, world.faculty, "Class moved")
    sub.refresh_from_db()
    assert req.refund_amount == Decimal("600.00") and sub.balance == Decimal("2000.00")
    txn = SubWalletTransaction.objects.get(pk=req.refund_txn_id)
    assert txn.description.startswith(f"Demonstration refund (full) – {world.equipment.code}")
    note = next(c for c in sent if c["code"] == "demo_request_cancelled_email")
    assert "₹600.00" in note["message"]


@pytest.mark.django_db
def test_reject_charges_nothing_and_curtail_charges_approved_duration(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    sub = fund_faculty(world.faculty, world.dept, Decimal("5000.00"))
    rejected = demo.decide(demo.create_request(world.faculty, _payload(world)), world.oic, {"action": "reject", "remarks": "Down"})
    assert rejected.status == DemoStatus.REJECTED and not rejected.wallet_txn_id
    sub.refresh_from_db()
    assert sub.balance == Decimal("5000.00")
    req = demo.create_request(world.faculty, _payload(world, requested_duration_minutes=180))
    req = demo.decide(
        req, world.oic, {"action": "approve", "approved_duration_minutes": 60, "reason_code": "INSTRUMENT_TIME"}
    )
    sub.refresh_from_db()
    assert req.curtailed and req.charge_amount == Decimal("600.00") and sub.balance == Decimal("4400.00")


@pytest.mark.django_db
def test_insufficient_balance_shown_before_submitting(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    fund_faculty(world.faculty, world.dept, Decimal("100.00"))
    faculty = client_for(world.faculty)
    quote = faculty.get(f"{API}/equipment/{world.equipment.equipment_id}/demo-quote/", {"purpose": "OTHER", "minutes": 120})
    assert quote.status_code == 200
    assert quote.data["amount"] == "1200.00" and quote.data["rate_per_hour"] == "600.00"
    assert quote.data["wallet_balance"] == "100.00" and quote.data["wallet_label"] == "Chemistry sub-wallet"
    assert "Insufficient wallet balance" in quote.data["balance_error"]
    resp = faculty.post(f"{API}/demo-requests/", _payload(world, purpose="OTHER"), format="json")
    assert resp.status_code == 400 and resp.data["code"] == "insufficient_balance"
    assert "Required: ₹1200.00" in resp.data["detail"]
    no_wallet = faculty.get(f"{API}/equipment/{world.other_equipment.equipment_id}/demo-quote/", {"minutes": 60})
    assert no_wallet.status_code == 200 and no_wallet.data["rate_available"] is False


@pytest.mark.django_db
def test_max_hours_enforced(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    fund_faculty(world.faculty, world.dept, Decimal("9000.00"))
    resp = client_for(world.faculty).post(f"{API}/demo-requests/", _payload(world, requested_duration_minutes=240), format="json")
    assert resp.status_code == 400 and resp.data["code"] == "over_max_duration"
    quote = client_for(world.faculty).get(f"{API}/equipment/{world.equipment.equipment_id}/demo-quote/", {"minutes": 240})
    assert quote.data["over_max"] is True and quote.data["demo_max_minutes"] == 180
    detail = client_for(world.faculty).get(f"{API}/equipment/{world.equipment.equipment_id}/")
    assert detail.data["demo_max_minutes"] == 180 and detail.data["charged_at_internal_rate"] is True


@pytest.mark.django_db
def test_oic_sets_rate_only_when_no_internal_rate_exists(world, sent):
    sub = fund_faculty(world.faculty, world.dept, Decimal("5000.00"))
    req = demo.create_request(world.faculty, _payload(world, requested_duration_minutes=60))
    assert req.charge_mode == "WALLET" and req.charge_amount == 0
    with pytest.raises(TrainingError) as exc:
        demo.decide(req, world.oic, {"action": "approve"})
    assert exc.value.code == "rate_required"
    req = demo.decide(req, world.oic, {"action": "approve", "rate_per_hour": "450"})
    sub.refresh_from_db()
    assert req.charge_amount == Decimal("450.00") and sub.balance == Decimal("4550.00")


@pytest.mark.django_db
def test_oic_quote_hides_faculty_balance(world, sent):
    internal_rate_profile(world.equipment, "600.00")
    fund_faculty(world.faculty, world.dept, Decimal("100.00"))
    SubWallet.objects.update(balance=Decimal("5000.00"))
    req = demo.create_request(world.faculty, _payload(world, requested_duration_minutes=60))
    SubWallet.objects.update(balance=Decimal("100.00"))
    resp = client_for(world.oic).get(
        f"{API}/equipment/{world.equipment.equipment_id}/demo-quote/", {"request_id": req.pk, "purpose": "COURSE", "minutes": 60}
    )
    assert resp.status_code == 200 and "wallet_balance" not in resp.data
    assert resp.data["balance_error"] == "The faculty member's wallet balance is not enough for this charge."
    assert client_for(world.other_oic).get(
        f"{API}/equipment/{world.equipment.equipment_id}/demo-quote/", {"request_id": req.pk}
    ).status_code == 403


@pytest.mark.django_db
def test_equipment_list_filters_by_department(world):
    empty_dept = make_department(name="Empty Dept")
    third = make_equipment(world.dept, name="AAS")
    faculty = client_for(world.faculty)
    resp = faculty.get(f"{API}/equipment/", {"department_id": world.dept.pk})
    assert resp.status_code == 200
    assert {r["equipment_id"] for r in resp.data["results"]} == {world.equipment.equipment_id, third.equipment_id}
    facets = {d["id"]: d["equipment_count"] for d in resp.data["departments"]}
    assert facets[world.dept.pk] == 2 and facets[world.other_dept.pk] == 1 and empty_dept.pk not in facets
    assert resp.data["demo_terms"]["demo_max_minutes"] == 180
    assert faculty.get(f"{API}/equipment/", {"department_id": empty_dept.pk}).data["results"] == []
    assert len(faculty.get(f"{API}/equipment/", {"department_id": "all"}).data["results"]) == 3
    assert faculty.get(f"{API}/equipment/", {"department_id": "x"}).status_code == 400
