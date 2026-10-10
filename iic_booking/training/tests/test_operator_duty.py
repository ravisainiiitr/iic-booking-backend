import uuid
from datetime import time, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, DailySlot, SlotStatus
from iic_booking.training import certification, duty, roster
from iic_booking.training.duty_fairness import rank
from iic_booking.training.models import (
    AwardStatus,
    CertificationAward,
    CertificationLevel,
    DutyAllocation,
    DutyShift,
    DutyStatus,
    OperatorPolicy,
    OperatorRosterEntry,
    PolicyScope,
    RosterSource,
    RosterStatus,
    ShiftStatus,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, future_day, internal_rate_profile, make_slots, make_user

PASS_ITEMS = [
    {"key": k, "passed": True}
    for k in ("safety", "sop_startup", "sample_prep", "calibration", "acquisition", "data_handling", "troubleshooting", "shutdown", "emergency")
]


@pytest.fixture
def l1(db):
    level, _ = CertificationLevel.objects.get_or_create(code="CERT_L1", defaults={"name": "Certified operator (L1)", "rank": 20})
    CertificationLevel.objects.filter(pk=level.pk).update(is_active=True, rank=20, default_validity_months=24)
    level.refresh_from_db()
    return level


def certify(world, user, level):
    return certification.issue(user, world.equipment, level, world.oic)


def operators(world, l1, n=3):
    out = []
    for i in range(n):
        u = make_user(user_type=UserType.STUDENT, department=world.dept, supervisor=world.faculty, name=f"Operator {i}")
        certify(world, u, l1)
        out.append(u)
    return out


def plan_payload(world, day, h1=9, h2=13, **extra):
    return {
        "equipment_id": world.equipment.equipment_id,
        "mode": "range",
        "date_from": day.isoformat(),
        "date_to": day.isoformat(),
        "time_from": f"{h1:02d}:00",
        "time_to": f"{h2:02d}:00",
        "weekdays": [0, 1, 2, 3, 4, 5, 6],
        **extra,
    }


# ---------------------------------------------------------------------------
# Pure fair rotation
# ---------------------------------------------------------------------------
def _cand(uid, term=0, **kw):
    return {
        "user_id": uid,
        "eligible_basis": True,
        "term_minutes": term,
        "week_minutes": {},
        "allocations_term": 0,
        "last_duty_end": None,
        "next_duty_start": None,
        "max_hours_week": None,
        "faculty_id": kw.pop("faculty_id", uid),
        "department_id": kw.pop("department_id", 1),
        **kw,
    }


POLICY = {
    "weights": {"load": 40, "rotation": 15, "rotation_full_days": 30, "faculty_share": 15, "department_share": 10, "repeat": 5},
    "max_hours_week": 12,
    "max_hours_term": 150,
    "cooling_days": 2,
}


def test_rank_prefers_fewer_hours_and_blocks_caps_and_cooling():
    now = timezone.now()
    start = now + timedelta(days=3)
    proposal = {"start": start, "end": start + timedelta(hours=4), "minutes": 240, "minutes_by_week": {duty.week_key(start): 240}}
    rows = rank(
        [
            _cand(1, term=600, last_duty_end=now - timedelta(days=20)),
            _cand(2, term=60, last_duty_end=now - timedelta(days=20)),
            _cand(3, term=0, week_minutes={duty.week_key(start): 600}),
            _cand(4, term=0, last_duty_end=start - timedelta(days=1)),
            _cand(5, eligible_basis=False, basis_reason="Certification expired"),
            _cand(6, term=150 * 60),
        ],
        proposal,
        POLICY,
        now=now,
    )
    by_id = {r["user_id"]: r for r in rows}
    assert by_id[2]["rank"] == 1 and "Next in the fair rotation" in by_id[2]["reasons"]
    assert by_id[1]["rank"] == 2
    assert any("Weekly cap" in b for b in by_id[3]["blocked"]) and by_id[3]["rank"] is None
    assert any("Cooling period" in b for b in by_id[4]["blocked"])
    assert by_id[5]["hard_blocked"] == ["Certification expired"]
    assert any("Term cap" in b for b in by_id[6]["blocked"])
    assert [r["user_id"] for r in rows][:2] == [2, 1]


def test_rank_penalises_over_represented_faculty_group():
    now = timezone.now()
    start = now + timedelta(days=3)
    proposal = {"start": start, "end": start + timedelta(hours=2), "minutes": 120, "minutes_by_week": {}}
    rows = rank(
        [
            _cand(1, term=300, faculty_id=10),
            _cand(2, term=300, faculty_id=10),
            _cand(3, term=300, faculty_id=10),
            _cand(4, term=300, faculty_id=20),
        ],
        proposal,
        POLICY,
        now=now,
    )
    by_id = {r["user_id"]: r for r in rows}
    assert by_id[4]["priority"] == by_id[1]["priority"]  # equal shares of hours and members → no penalty
    rows = rank(
        [_cand(1, term=900, faculty_id=10), _cand(2, term=900, faculty_id=10), _cand(3, term=0, faculty_id=20), _cand(4, term=0, faculty_id=20)],
        proposal,
        POLICY,
        now=now,
    )
    by_id = {r["user_id"]: r for r in rows}
    assert by_id[1]["breakdown"]["faculty_share"] < 0
    assert {by_id[3]["rank"], by_id[4]["rank"]} == {1, 2}


# ---------------------------------------------------------------------------
# Assessment and certificates
# ---------------------------------------------------------------------------
def test_oic_assessment_issues_certificate_pdf_and_public_verification(world, l1):
    oic = client_for(world.oic)
    payload = {
        "equipment_id": world.equipment.equipment_id,
        "user_id": world.student.id,
        "target_level": "CERT_L1",
        "theory_score_pct": 85,
        "practical_items": PASS_ITEMS,
    }
    r = oic.post(f"{API}/assessments/", payload, format="json")
    assert r.status_code == 400 and r.data["code"] == "prerequisite_missing"
    r = oic.post(f"{API}/assessments/", {**payload, "prerequisite_waiver_reason": "Trained at the vendor site"}, format="json")
    assert r.status_code == 201, r.data
    assert r.data["result"] == "PASS" and r.data["award_id"] and r.data["certificate_no"].startswith("IIC-")
    award = CertificationAward.objects.get(pk=r.data["award_id"])
    assert award.level.code == "CERT_L1" and award.valid_until is not None

    pdf = client_for(world.student).get(f"{API}/certifications/{award.pk}/certificate.pdf")
    assert pdf.status_code == 200 and pdf["Content-Type"] == "application/pdf" and pdf.content[:4] == b"%PDF"
    assert client_for(world.student2).get(f"{API}/certifications/{award.pk}/certificate.pdf").status_code == 403

    public = client_for(None).get(f"{API}/verify/{award.verify_token}/")
    assert public.status_code == 200 and public.data["valid"] is True
    assert public.data["holder"] == "Student One" and "email" not in str(public.data)
    assert client_for(None).get(f"{API}/verify/not-a-token/").status_code == 404

    r = oic.post(f"{API}/certifications/{award.pk}/suspend/", {"reason": "Unsafe handling"}, format="json")
    assert r.status_code == 200 and r.data["status"] == AwardStatus.SUSPENDED
    assert client_for(None).get(f"{API}/verify/{award.verify_token}/").data["valid"] is False
    assert oic.post(f"{API}/certifications/{award.pk}/reinstate/", {}, format="json").data["code"] == "reason_required"
    assert oic.post(f"{API}/certifications/{award.pk}/reinstate/", {"reason": "Retrained"}, format="json").data["status"] == AwardStatus.ACTIVE
    detail = client_for(world.oic).get(f"{API}/certifications/{award.pk}/").data
    assert [h["action"] for h in detail["history"]][-2:] == ["award.suspend", "award.reinstate"]


def test_critical_item_failure_needs_retake_and_operator_assessment_needs_oic_sign_off(world, l1):
    certify_trained = CertificationLevel.objects.get(code="TRAINED")
    certification.issue(world.student, world.equipment, certify_trained, world.oic)
    lab = client_for(world.operator)
    items = [dict(i, passed=(i["key"] != "safety")) for i in PASS_ITEMS]
    base = {"equipment_id": world.equipment.equipment_id, "user_id": world.student.id, "target_level": "CERT_L1", "theory_score_pct": 90}
    r = lab.post(f"{API}/assessments/", {**base, "practical_items": items}, format="json")
    assert r.status_code == 201 and r.data["result"] == "RETAKE" and not r.data["award_id"]

    r = lab.post(f"{API}/assessments/", {**base, "practical_items": PASS_ITEMS}, format="json")
    assert r.data["result"] == "PASS" and r.data["awaiting_sign_off"] is True
    assert client_for(world.operator).post(f"{API}/assessments/{r.data['id']}/sign-off/").status_code == 403
    signed = client_for(world.oic).post(f"{API}/assessments/{r.data['id']}/sign-off/")
    assert signed.status_code == 200 and signed.data["award_id"]
    # the lower TRAINED award is superseded by the operator certificate
    assert CertificationAward.objects.get(user=world.student, level=certify_trained).status == AwardStatus.SUPERSEDED
    assert client_for(world.oic).post(f"{API}/assessments/", {**base, "user_id": world.oic.id, "practical_items": PASS_ITEMS}, format="json").status_code in (400, 403)


def test_housekeeping_expires_and_reminds_once(world, l1):
    award = certify(world, world.student, l1)
    CertificationAward.objects.filter(pk=award.pk).update(valid_until=timezone.now() + timedelta(days=10))
    first = certification.housekeeping()
    assert first["reminded"] == 1 and certification.housekeeping()["reminded"] == 0
    CertificationAward.objects.filter(pk=award.pk).update(valid_until=timezone.now() - timedelta(days=1))
    assert certification.housekeeping()["expired"] == 1
    award.refresh_from_db()
    assert award.status == AwardStatus.EXPIRED


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------
def test_roster_syncs_certified_and_ta_and_manual_requires_reason(world, l1):
    certify(world, world.student, l1)
    oic = client_for(world.oic)
    rows = oic.get(f"{API}/roster/?equipment_id={world.equipment.equipment_id}").data["results"]
    assert [r["user"]["id"] for r in rows] == [world.student.id] and rows[0]["eligible"] and rows[0]["source"] == RosterSource.AWARD
    r = oic.post(f"{API}/roster/", {"equipment_id": world.equipment.equipment_id, "user_id": world.student2.id}, format="json")
    assert r.data["code"] == "reason_required"
    r = oic.post(f"{API}/roster/", {"equipment_id": world.equipment.equipment_id, "user_id": world.student2.id, "reason": "Covering summer term"}, format="json")
    assert r.status_code == 201 and r.data["eligible"]
    assert client_for(world.other_oic).get(f"{API}/roster/?equipment_id={world.equipment.equipment_id}").status_code == 403
    entry = OperatorRosterEntry.objects.get(user=world.student2)
    assert oic.post(f"{API}/roster/{entry.pk}/pause/", {"reason": "Exams"}, format="json").data["eligible"] is False
    assert client_for(world.student2).get(f"{API}/bootstrap/").data["menus"]["my_duty"] is False
    assert client_for(world.student).get(f"{API}/bootstrap/").data["menus"]["my_duty"] is True


# ---------------------------------------------------------------------------
# Duty allocation
# ---------------------------------------------------------------------------
def test_plan_ranks_roster_and_override_needs_reason(world, l1):
    a, b, c = operators(world, l1)
    day = future_day(12)
    # b already worked a long block this term → a or c is next
    past = DutyAllocation.objects.create(equipment=world.equipment, operator=b, status=DutyStatus.COMPLETED, planned_minutes=600)
    DutyShift.objects.create(
        allocation=past, equipment=world.equipment, operator=b, start_at=timezone.now() - timedelta(days=5),
        end_at=timezone.now() - timedelta(days=5) + timedelta(hours=10), status=ShiftStatus.COMPLETED, operated_minutes=600,
    )
    oic = client_for(world.oic)
    plan = oic.post(f"{API}/duty/plan/", plan_payload(world, day), format="json")
    assert plan.status_code == 200, plan.data
    ranking = plan.data["ranking"]
    assert ranking[0]["user_id"] in (a.id, c.id) and ranking[-1]["user_id"] == b.id
    assert plan.data["shifts"][0]["minutes"] == 240

    r = oic.post(f"{API}/duty/allocations/", {**plan_payload(world, day), "operator_id": b.id}, format="json")
    assert r.status_code == 400 and r.data["code"] == "override_reason_required"
    r = oic.post(f"{API}/duty/allocations/", {**plan_payload(world, day), "operator_id": b.id, "override_reason": "Only b is trained on EBSD"}, format="json")
    assert r.status_code == 201, r.data
    assert r.data["override_reason"] and r.data["suggested_rank"] == 3 and r.data["fairness_snapshot"]["ranking"]
    assert client_for(world.other_oic).post(f"{API}/duty/plan/", plan_payload(world, day), format="json").status_code == 403


def test_email_token_confirm_once_and_portal_decline(world, l1, mailoutbox):
    a, b, _ = operators(world, l1)
    day = future_day(14)
    oic = client_for(world.oic)
    top = oic.post(f"{API}/duty/plan/", plan_payload(world, day), format="json").data["ranking"][0]["user_id"]
    r = oic.post(f"{API}/duty/allocations/", {**plan_payload(world, day), "operator_id": top}, format="json")
    assert r.status_code == 201 and r.data["status"] == DutyStatus.PENDING and r.data["confirm_by"]
    alloc = DutyAllocation.objects.get(pk=r.data["id"])
    token = duty.make_token(alloc)

    anon = client_for(None)
    view = anon.get(f"{API}/duty/respond/?token={token}")
    assert view.status_code == 200 and view.data["can_respond"] is True
    alloc.refresh_from_db()
    assert alloc.status == DutyStatus.PENDING  # GET never changes state
    done = anon.post(f"{API}/duty/respond/", {"token": token, "action": "confirm"}, format="json")
    assert done.status_code == 200 and done.data["status"] == DutyStatus.CONFIRMED
    alloc.refresh_from_db()
    assert alloc.response_channel == "EMAIL"
    assert anon.post(f"{API}/duty/respond/", {"token": token, "action": "decline"}, format="json").status_code == 410
    assert anon.get(f"{API}/duty/respond/?token=garbage").status_code == 400

    # a second allocation the other operator declines in the portal
    other = a if top != a.id else b
    day2 = future_day(15)
    r2 = oic.post(
        f"{API}/duty/allocations/", {**plan_payload(world, day2), "operator_id": other.id, "override_reason": "Swap"}, format="json"
    )
    assert r2.status_code == 201, r2.data
    me = client_for(other)
    assert me.post(f"{API}/duty/allocations/{r2.data['id']}/decline/", {}, format="json").data["code"] == "reason_required"
    d = me.post(f"{API}/duty/allocations/{r2.data['id']}/decline/", {"reason": "Conference travel"}, format="json")
    assert d.data["status"] == DutyStatus.DECLINED and all(s["status"] == ShiftStatus.RELEASED for s in d.data["shifts"])
    assert client_for(world.student2).post(f"{API}/duty/allocations/{r.data['id']}/confirm/").status_code == 403


def test_conflicts_block_overlapping_duty_and_own_booking(world, l1):
    (a,) = operators(world, l1, n=1)
    day = future_day(16)
    oic = client_for(world.oic)
    assert oic.post(f"{API}/duty/allocations/", {**plan_payload(world, day), "operator_id": a.id}, format="json").status_code == 201
    r = oic.post(f"{API}/duty/allocations/", {**plan_payload(world, day, 11, 15), "operator_id": a.id, "override_reason": "x"}, format="json")
    assert r.status_code == 400 and r.data["code"] == "conflicts"

    day2 = future_day(17)
    slots = make_slots(world.other_equipment, day=day2, hours=range(10, 12))
    profile = internal_rate_profile(world.other_equipment, user_type=UserType.STUDENT)
    booking = Booking.objects.create(
        user=a, equipment=world.other_equipment, charge_profile=profile, status=BookingStatus.BOOKED,
        total_charge=Decimal("10.00"), total_time_minutes=120, virtual_booking_id=f"TR{uuid.uuid4().hex[:8]}",
        user_type_snapshot=UserType.STUDENT,
    )
    DailySlot.objects.filter(pk__in=[s.pk for s in slots]).update(booking=booking, status=SlotStatus.BOOKED)
    plan = oic.post(f"{API}/duty/plan/", {**plan_payload(world, day2), "operator_id": a.id}, format="json").data
    assert {c["code"] for c in plan["shifts"][0]["conflicts"]} >= {"own_booking", "no_slots"}
    assert plan["blocking"]


def test_unconfirmed_duty_is_released_and_oic_escalated(world, l1):
    (a, _b) = operators(world, l1, n=2)
    day = future_day(18)
    alloc = duty.create(world.oic, {**plan_payload(world, day), "operator_id": a.id, "override_reason": "test"})
    DutyAllocation.objects.filter(pk=alloc.pk).update(confirm_by=timezone.now() - timedelta(minutes=1))
    out = duty.housekeeping()
    assert out["released"] == 1
    alloc.refresh_from_db()
    assert alloc.status == DutyStatus.EXPIRED and alloc.escalated_at
    assert set(alloc.shifts.values_list("status", flat=True)) == {ShiftStatus.RELEASED}


def test_reminder_sent_before_deadline(world, l1):
    (a,) = operators(world, l1, n=1)
    alloc = duty.create(world.oic, {**plan_payload(world, future_day(19)), "operator_id": a.id})
    DutyAllocation.objects.filter(pk=alloc.pk).update(confirm_by=timezone.now() + timedelta(hours=2))
    assert duty.housekeeping()["reminded"] == 1
    assert duty.housekeeping()["reminded"] == 0


def _confirmed_now(world, operator, *, start_offset=-30, hours=2):
    alloc = duty.create(
        world.oic, {**plan_payload(world, future_day(20)), "operator_id": operator.id, "requires_confirmation": False, "override_reason": "t"}
    )
    shift = alloc.shifts.get()
    start = timezone.now() + timedelta(minutes=start_offset)
    DutyShift.objects.filter(pk=shift.pk).update(start_at=start, end_at=start + timedelta(hours=hours))
    shift.refresh_from_db()
    return alloc, shift


def test_check_in_out_records_hours_and_completes_allocation(world, l1):
    (a,) = operators(world, l1, n=1)
    alloc, shift = _confirmed_now(world, a)
    me = client_for(a)
    assert client_for(world.student2).post(f"{API}/duty/shifts/{shift.pk}/check-in/").status_code == 403
    r = me.post(f"{API}/duty/shifts/{shift.pk}/check-in/")
    assert r.status_code == 200 and r.data["status"] == ShiftStatus.CHECKED_IN
    # time before "30 minutes ahead of the start" does not count
    DutyShift.objects.filter(pk=shift.pk).update(check_in_at=timezone.now() - timedelta(minutes=90))
    r = me.post(f"{API}/duty/shifts/{shift.pk}/check-out/")
    assert r.data["status"] == ShiftStatus.COMPLETED and 59 <= r.data["operated_minutes"] <= 61 and r.data["hours_source"] == "CHECKIN"
    alloc.refresh_from_db()
    assert alloc.status == DutyStatus.COMPLETED
    v = client_for(world.oic).post(f"{API}/duty/shifts/{shift.pk}/verify/", {"operated_minutes": 100, "remarks": "Stayed for the run"}, format="json")
    assert v.data["operated_minutes"] == 100 and v.data["hours_source"] == "OIC" and v.data["verified_by"]["id"] == world.oic.id
    assert CertificationAward.objects.get(user=a, status=AwardStatus.ACTIVE).last_used_at is not None


def test_hours_derived_from_completed_bookings_and_accounting(world, l1):
    a, b = operators(world, l1, n=2)
    alloc, shift = _confirmed_now(world, a, start_offset=-300, hours=3)
    profile = internal_rate_profile(world.equipment, user_type=UserType.STUDENT)
    day = timezone.localtime(shift.start_at).date()
    from iic_booking.equipment.models import SlotMaster

    master = SlotMaster.objects.create(
        equipment=world.equipment, slot_number=99, slot_name="X", open_time=time(0, 0), close_time=time(23, 59)
    )
    booking = Booking.objects.create(
        user=world.student2, equipment=world.equipment, charge_profile=profile, status=BookingStatus.COMPLETED,
        completed_at=timezone.now(), total_charge=Decimal("10.00"), total_time_minutes=60,
        virtual_booking_id=f"TR{uuid.uuid4().hex[:8]}", user_type_snapshot=UserType.STUDENT,
    )
    DailySlot.objects.create(
        slot_master=master, date=day, start_datetime=shift.start_at + timedelta(minutes=30),
        end_datetime=shift.start_at + timedelta(minutes=90), status=SlotStatus.BOOKED, booking=booking,
    )
    OperatorPolicy.objects.create(scope=PolicyScope.GLOBAL, version=99, is_active=True, duty_hourly_rate=Decimal("200.00"))
    out = duty.housekeeping()
    assert out["closed_booking"] == 1
    shift.refresh_from_db()
    assert shift.status == ShiftStatus.COMPLETED and shift.operated_minutes == 60 and shift.hours_source == "BOOKING"

    # a second, still-unverified shift for b counts as pending
    _alloc_b, shift_b = _confirmed_now(world, b, start_offset=-200, hours=1)
    oic = client_for(world.oic)
    acc = oic.get(f"{API}/duty/accounting/?group_by=operator").data
    rows = {r["key"]: r for r in acc["rows"]}
    assert rows[str(a.id)]["operated_hours"] == 1.0 and rows[str(a.id)]["confirmed_hours"] == 3.0
    assert rows[str(b.id)]["pending_hours"] == 1.0
    assert acc["totals"]["operators"] == 2
    csv_resp = oic.get(f"{API}/duty/accounting/export/")
    assert csv_resp.status_code == 200 and b"Operated hours" in csv_resp.content
    live = oic.get(f"{API}/duty/live/").data
    assert live["pending_verification_count"] == 1
    stmt = client_for(a).get(f"{API}/duty/statement/").data
    assert stmt["operator"]["id"] == a.id and stmt["lines"][0]["operated_hours"] == 1.0
    assert client_for(a).get(f"{API}/duty/statement/?operator_id={b.id}").status_code == 403
    assert client_for(world.other_oic).get(f"{API}/duty/accounting/").data["totals"]["operators"] == 0
    me = client_for(a).get(f"{API}/duty/me/").data
    assert me["hours"]["totals"]["operated_hours"] == 1.0


def test_cancel_requires_reason_and_frees_shifts(world, l1):
    (a,) = operators(world, l1, n=1)
    alloc = duty.create(world.oic, {**plan_payload(world, future_day(21)), "operator_id": a.id})
    oic = client_for(world.oic)
    assert oic.post(f"{API}/duty/allocations/{alloc.pk}/cancel/", {}, format="json").data["code"] == "reason_required"
    r = oic.post(f"{API}/duty/allocations/{alloc.pk}/cancel/", {"reason": "Instrument down"}, format="json")
    assert r.data["status"] == DutyStatus.CANCELLED and {s["status"] for s in r.data["shifts"]} == {ShiftStatus.CANCELLED}


def test_recurring_mode_skips_holidays_and_unchosen_weekdays(world, l1):
    from iic_booking.equipment.models import Holiday

    start = future_day(30)
    monday = start + timedelta(days=(7 - start.weekday()) % 7)
    Holiday.objects.create(date=monday + timedelta(days=7), reason="Founders' Day")
    windows, skipped = duty.build_windows(
        world.equipment,
        {"mode": "recurring", "date_from": monday.isoformat(), "date_to": (monday + timedelta(days=20)).isoformat(),
         "time_from": "14:00", "time_to": "16:00", "weekdays": [0, 2]},
    )
    assert len(windows) == 5 and skipped == [{"date": (monday + timedelta(days=7)).isoformat(), "reason": "Holiday: Founders' Day"}]
    assert all(timezone.localtime(w["start"]).weekday() in (0, 2) for w in windows)


def test_slot_mode_merges_contiguous_slots(world, l1):
    day = future_day(22)
    slots = make_slots(world.equipment, day=day, hours=[9, 10, 13])
    windows, _ = duty.build_windows(world.equipment, {"mode": "slots", "slot_ids": [s.id for s in slots]})
    assert [(timezone.localtime(w["start"]).hour, timezone.localtime(w["end"]).hour) for w in windows] == [(9, 11), (13, 14)]
    other = make_slots(world.other_equipment, day=day, hours=[9])
    with pytest.raises(Exception):
        duty.build_windows(world.equipment, {"mode": "slots", "slot_ids": [other[0].id]})


def test_operator_policy_versions_and_scope(world):
    admin = client_for(world.admin)
    r = admin.post(f"{API}/operator-policy/", {"scope": "GLOBAL", "duty_max_hours_week": 10, "duty_cooling_days": 3}, format="json")
    assert r.status_code == 201 and r.data["duty_max_hours_week"] == 10
    r2 = admin.post(f"{API}/operator-policy/", {"scope": "GLOBAL", "duty_confirm_hours": 12}, format="json")
    assert r2.data["version"] == r.data["version"] + 1 and r2.data["duty_max_hours_week"] == 10
    assert admin.post(f"{API}/operator-policy/", {"scope": "GLOBAL", "duty_max_hours_week": 500}, format="json").status_code == 400
    oic = client_for(world.oic)
    assert oic.post(f"{API}/operator-policy/", {"scope": "GLOBAL", "duty_max_hours_week": 5}, format="json").status_code == 403
    r3 = oic.post(f"{API}/operator-policy/", {"scope": "EQUIPMENT", "equipment_id": world.equipment.equipment_id, "duty_max_hours_week": 6}, format="json")
    assert r3.status_code == 201
    eff = oic.get(f"{API}/operator-policy/?equipment_id={world.equipment.equipment_id}").data["effective"]
    assert eff["duty_max_hours_week"] == 6 and eff["duty_confirm_hours"] == 12
    assert client_for(world.other_oic).post(
        f"{API}/operator-policy/", {"scope": "EQUIPMENT", "equipment_id": world.equipment.equipment_id, "duty_max_hours_week": 6}, format="json"
    ).status_code == 403


def test_workspace_summary_counts_duty(world, l1):
    (a,) = operators(world, l1, n=1)
    duty.create(world.oic, {**plan_payload(world, future_day(23)), "operator_id": a.id})
    data = client_for(world.oic).get(f"{API}/workspace/summary/").data
    assert data["duty_awaiting_confirmation"] == 1 and data["duty_hours_to_verify"] == 0


# ---------------------------------------------------------------------------
# Shortlisting: cooling period for recent seats
# ---------------------------------------------------------------------------
def test_scoring_applies_recent_selection_and_group_repeat_penalties():
    from iic_booking.training import scoring
    from iic_booking.training.models import DEFAULT_SCORING_WEIGHTS

    weights = {**DEFAULT_SCORING_WEIGHTS, "recent_selection_penalty": -20.0, "group_repeat_per_selection": -5.0}
    base = {"department_key": "1", "eligible": True}
    cands = [
        {**base, "nomination_id": 1, "factors": {"recent_selection": True, "group_recent_selections": 0}},
        {**base, "nomination_id": 2, "factors": {"recent_selection": False, "group_recent_selections": 2}},
        {**base, "nomination_id": 3, "factors": {}},
    ]
    out = scoring.score_candidates(cands, {"weights": weights})
    assert out[1]["breakdown"]["recent_selection"] == -20.0
    assert out[2]["breakdown"]["group_repeat"] == -10.0
    assert out[1]["total"] < out[3]["total"] and out[2]["total"] < out[3]["total"]
    legacy = scoring.score_candidates(cands, {"weights": DEFAULT_SCORING_WEIGHTS})
    assert "recent_selection" not in legacy[1]["breakdown"]


def test_roster_basis_rejects_expired_and_unused_entries(world, l1):
    award = certify(world, world.student, l1)
    roster.sync([world.equipment.equipment_id])
    entry = OperatorRosterEntry.objects.get(user=world.student)
    assert roster.basis(entry)[0] is True
    CertificationAward.objects.filter(pk=award.pk).update(status=AwardStatus.EXPIRED)
    ok, why = roster.basis(entry)
    assert ok is False and "expired" in why.lower()
    OperatorRosterEntry.objects.filter(pk=entry.pk).update(status=RosterStatus.PAUSED, status_reason="Leave")
    entry.refresh_from_db()
    assert roster.basis(entry) == (False, "Paused by the OIC: Leave")
