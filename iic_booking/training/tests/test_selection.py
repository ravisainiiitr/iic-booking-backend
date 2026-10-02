"""Calls, nominations, eligibility gates, shortlist preview/publish, seat confirmation, promotion, appeals."""

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.training import notify, selection
from iic_booking.training.errors import TrainingError
from iic_booking.training.models import (
    AwardStatus,
    CallStatus,
    CertificationAward,
    CertificationLevel,
    NominationCall,
    NominationStatus,
    Registration,
    RunStatus,
    TrainingNomination,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, join_wallet, make_department, make_user


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    calls = []
    monkeypatch.setattr(notify, "send", lambda code, recipients, **kw: calls.append((code, [r.id for r in recipients])))
    return calls


def _open_call(world, seats=2, **extra):
    return selection.open_call(
        world.oic,
        {"equipment_id": world.equipment.equipment_id, "seats": seats, "deadline": (timezone.now() + timedelta(days=5)).isoformat(), **extra},
    )


def _student(world, faculty=None, dept=None, **kw):
    faculty = faculty or world.faculty
    kw.setdefault("program_end_date", timezone.localdate() + timedelta(days=900))
    return make_user(user_type=UserType.STUDENT, department=dept or faculty.department, supervisor=faculty, **kw)


def _nominate(world, call, student, faculty=None, need="THESIS_CRITICAL", confirm=True):
    n = selection.nominate(
        faculty or student.supervisor,
        {"call_id": call.pk, "student_id": student.pk, "need_category": need, "justification": "Needs FE-SEM imaging for thesis chapter 3."},
    )
    if confirm:
        selection.confirm_interest(n, student)
    return n


def _close(call, closed_at=None):
    closed_at = closed_at or timezone.now() - timedelta(minutes=5)
    NominationCall.objects.filter(pk=call.pk).update(status=CallStatus.CLOSED, closed_at=closed_at)
    call.refresh_from_db()
    return call


@pytest.mark.django_db
def test_only_supervisor_or_wallet_faculty_can_nominate(world):
    call = _open_call(world)
    with pytest.raises(TrainingError) as exc:
        selection.nominate(world.faculty, {"call_id": call.pk, "student_id": world.outsider.pk, "need_category": "EXPLORATORY", "justification": "x" * 30})
    assert exc.value.status == 403
    join_wallet(world.outsider, world.faculty)
    n = selection.nominate(world.faculty, {"call_id": call.pk, "student_id": world.outsider.pk, "need_category": "EXPLORATORY", "justification": "x" * 30})
    assert n.status == NominationStatus.SUBMITTED
    with pytest.raises(TrainingError):
        selection.nominate(world.faculty, {"call_id": call.pk, "student_id": world.outsider.pk, "need_category": "EXPLORATORY", "justification": "x" * 30})


@pytest.mark.django_db
def test_eligibility_gates(world, seed_levels):
    call = _open_call(world, seats=5)
    ok = _student(world, name="Eligible")
    on_hold = _student(world, name="On Hold", access_on_hold=True)
    leaving = _student(world, name="Leaving", program_end_date=timezone.localdate() + timedelta(days=40))
    unknown_end = _student(world, name="Unknown End", program_end_date=None)
    unconfirmed = _student(world, name="Unconfirmed")
    suspended = _student(world, name="Suspended")
    holder = _student(world, name="Already Trained")
    for st in (ok, on_hold, leaving, unknown_end, suspended, holder):
        _nominate(world, call, st)
    _nominate(world, call, unconfirmed, confirm=False)
    CertificationAward.objects.create(user=suspended, equipment=world.equipment, level=seed_levels, status=AwardStatus.SUSPENDED, awarded_at=timezone.now() - timedelta(days=400), suspended_at=timezone.now() - timedelta(days=30))
    CertificationAward.objects.create(user=holder, equipment=world.equipment, level=seed_levels, status=AwardStatus.ACTIVE, awarded_at=timezone.now() - timedelta(days=400))

    candidates, _ = selection.build_candidates(call, call.caps_snapshot["policy"])
    by_name = {c["student_name"]: c for c in candidates}
    assert by_name["Eligible"]["eligible"]
    assert not by_name["On Hold"]["eligible"]
    assert any("Programme ends" in r for r in by_name["Leaving"]["ineligible_reasons"])
    assert by_name["Unknown End"]["eligible"] and "Programme end date unknown" in by_name["Unknown End"]["flags"]
    assert "Student has not confirmed interest" in by_name["Unconfirmed"]["ineligible_reasons"]
    assert "Active suspension on this equipment" in by_name["Suspended"]["ineligible_reasons"]
    assert any("Already holds" in r for r in by_name["Already Trained"]["ineligible_reasons"])


@pytest.mark.django_db
def test_preview_publish_confirm_and_waitlist_promotion(world):
    call = _open_call(world, seats=2)
    f2 = make_user(user_type=UserType.FACULTY, department=world.dept, name="Prof. C")
    f3 = make_user(user_type=UserType.FACULTY, department=world.other_dept, name="Prof. D")
    a = _student(world, name="A")
    b = _student(world, name="B")  # same faculty as A → faculty cap
    c = _student(world, faculty=f2, name="C")
    d = _student(world, faculty=f3, name="D")
    na, nb = _nominate(world, call, a), _nominate(world, call, b, need="EXPLORATORY")
    nc, nd = _nominate(world, call, c, need="FUNDED_PROJECT"), _nominate(world, call, d, need="EXPLORATORY")

    run = selection.run_preview(call, world.oic, public_input="1234")
    assert run.status == RunStatus.DRAFT
    with pytest.raises(TrainingError) as exc:
        selection.publish(run, world.oic, public_input="1234")
    assert exc.value.code == "call_open"

    call = _close(call)
    run = selection.run_preview(call, world.oic, public_input="1234")
    entries = {e.nomination_id: e for e in run.entries.all()}
    # 2 seats → department cap is 1 (40%), so the second Chemistry nominee yields to Physics.
    assert entries[na.pk].outcome == "SELECTED" and entries[nd.pk].outcome == "SELECTED"
    assert entries[nc.pk].outcome == "WAITLISTED" and entries[nc.pk].constraint_note == "Department cap reached"
    assert entries[nb.pk].outcome == "WAITLISTED"

    run = selection.publish(run, world.oic, public_input="1234")
    assert run.status == RunStatus.PUBLISHED and run.appeal_deadline > timezone.now()
    assert selection.verify_run(run)["reproducible"]
    call.refresh_from_db()
    assert call.status == CallStatus.PUBLISHED
    na.refresh_from_db()
    assert na.status == NominationStatus.SELECTED and na.confirm_deadline > timezone.now()

    selection.accept_seat(na, a)
    assert Registration.objects.filter(event=call.event, user=a, status="CONFIRMED").exists()

    TrainingNomination.objects.filter(pk=nd.pk).update(confirm_deadline=timezone.now() - timedelta(minutes=1))
    assert selection.expire_unconfirmed() == 1
    nd.refresh_from_db()
    assert nd.status == NominationStatus.EXPIRED
    promoted = TrainingNomination.objects.get(promoted_from_waitlist=True)
    # B shares A's faculty (cap 1); C is from another group, so C gets the seat (department cap relaxed).
    assert promoted.pk == nc.pk and promoted.status == NominationStatus.SELECTED

    csv_text = selection.export_csv(run)
    assert "rank,student" in csv_text.splitlines()[0] and "seed" in csv_text


@pytest.mark.django_db
def test_stale_preview_and_public_input_required(world):
    call = _open_call(world, seats=1)
    _nominate(world, call, _student(world, name="A"))
    run = selection.run_preview(call, world.oic)
    call = _close(call, closed_at=timezone.now() + timedelta(seconds=1))
    with pytest.raises(TrainingError) as exc:
        selection.publish(run, world.oic, public_input="77")
    assert exc.value.code == "stale_preview"
    call = _close(call)
    run = selection.run_preview(call, world.oic)
    with pytest.raises(TrainingError) as exc:
        selection.publish(run, world.oic)
    assert exc.value.code == "public_input_required"


@pytest.mark.django_db
def test_override_requires_reason_and_is_flagged(world):
    call = _close(_open_call(world, seats=1))
    NominationCall.objects.filter(pk=call.pk).update(status=CallStatus.OPEN, deadline=timezone.now() + timedelta(days=1))
    call.refresh_from_db()
    n1 = _nominate(world, call, _student(world, name="High"))
    other_fac = make_user(user_type=UserType.FACULTY, department=world.other_dept)
    n2 = _nominate(world, call, _student(world, faculty=other_fac, name="Low"), need="EXPLORATORY")
    call = _close(call)
    run = selection.run_preview(call, world.oic, public_input="5")
    low = run.entries.get(nomination=n2)
    with pytest.raises(TrainingError) as exc:
        selection.override_entry(low, world.oic, outcome="SELECTED", reason="")
    assert exc.value.code == "reason_required"
    run = selection.override_entry(low, world.oic, outcome="SELECTED", reason="Only user of the cryo stage")
    low = run.entries.get(nomination=n2)
    assert low.outcome == "SELECTED" and low.overridden and low.override_reason
    assert run.entries.get(nomination=n1).outcome == "WAITLISTED"
    run = selection.publish(run, world.oic, public_input="5")
    assert run.entries.get(nomination=n2).overridden
    assert selection.verify_run(run)["reproducible"]


@pytest.mark.django_db
def test_appeal_window_and_decision(world):
    call = _open_call(world, seats=1)
    other_fac = make_user(user_type=UserType.FACULTY, department=world.other_dept)
    n1 = _nominate(world, call, _student(world, name="Top"))
    loser = _student(world, faculty=other_fac, name="Appellant")
    n2 = _nominate(world, call, loser, need="EXPLORATORY")
    call = _close(call)
    run = selection.publish(selection.run_preview(call, world.oic, public_input="9"), world.oic, public_input="9")
    entry = run.entries.get(nomination=n2)
    assert entry.outcome == "WAITLISTED"
    with pytest.raises(TrainingError):
        selection.submit_appeal(entry, world.student2, "I should have been selected because ...")
    appeal = selection.submit_appeal(entry, loser, "My thesis depends on this instrument in the next month.")
    with pytest.raises(TrainingError) as exc:
        selection.decide_appeal(appeal, world.operator, decision="OVERTURNED", note="ok")
    assert exc.value.status == 403
    appeal = selection.decide_appeal(appeal, world.oic, decision="OVERTURNED", note="Thesis timeline verified")
    n2.refresh_from_db()
    assert n2.status == NominationStatus.SELECTED and n2.selected_on_appeal
    assert n1.pk != n2.pk


@pytest.mark.django_db
def test_department_underrepresentation_from_certified_share(world, seed_levels):
    """Dept with demand but no certified users gets the reserved seat even with a lower base score."""
    call = _open_call(world, seats=5)
    certified_dept_students = [_student(world, name=f"Cert{i}") for i in range(3)]
    for st in certified_dept_students:
        CertificationAward.objects.create(user=st, equipment=world.equipment, level=CertificationLevel.objects.get(code="TRAINED"), status=AwardStatus.ACTIVE, awarded_at=timezone.now() - timedelta(days=500))
    rare_dept = make_department(name="Earth Sciences")
    rare_fac = make_user(user_type=UserType.FACULTY, department=rare_dept)
    rare = _student(world, faculty=rare_fac, dept=rare_dept, name="Rare")
    facs = [make_user(user_type=UserType.FACULTY, department=world.dept) for _ in range(6)]
    for i, f in enumerate(facs):
        _nominate(world, call, _student(world, faculty=f, name=f"Chem{i}"))
    n_rare = _nominate(world, call, rare, need="EXPLORATORY")
    call = _close(call)
    run = selection.run_preview(call, world.oic, public_input="3")
    assert str(rare_dept.pk) in run.inputs_snapshot["underrepresented_departments"]
    entry = run.entries.get(nomination=n_rare)
    assert entry.outcome == "SELECTED" and entry.seat_type == "RESERVED"


@pytest.mark.django_db
def test_call_api_permissions(world):
    deadline = (timezone.now() + timedelta(days=3)).isoformat()
    body = {"equipment_id": world.equipment.equipment_id, "seats": 2, "deadline": deadline}
    assert client_for(world.faculty).post(f"{API}/calls/", body, format="json").status_code == 403
    assert client_for(world.other_oic).post(f"{API}/calls/", body, format="json").status_code == 403
    assert client_for(world.operator).post(f"{API}/calls/", body, format="json").status_code == 403
    resp = client_for(world.temp_oic).post(f"{API}/calls/", body, format="json")
    assert resp.status_code == 201, resp.data
    cid = resp.data["id"]
    assert client_for(world.faculty).get(f"{API}/calls/?scope=open").data["results"][0]["id"] == cid
    assert client_for(world.faculty).post(f"{API}/calls/{cid}/shortlist/", {}, format="json").status_code == 403
    assert client_for(world.oic).post(f"{API}/calls/{cid}/shortlist/", {}, format="json").status_code == 201
    assert client_for(world.student).get(f"{API}/calls/{cid}/shortlist/").status_code == 403
