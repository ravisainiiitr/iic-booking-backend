"""Beginner / Expert competency per equipment: induction makes a Beginner, Beginners book only with an eligible
supervisor, an Expert assessment (approved by the OIC) makes them independent, designated trainers, supervised
sessions and the audit trail."""

from datetime import timedelta
from unittest import mock

import pytest
from dateutil.relativedelta import relativedelta
from django.utils import timezone

from iic_booking.equipment.models import BookingStatus, DailySlot
from iic_booking.training import authorisation, certification, competency
from iic_booking.training.models import (
    AuthorisationRequest,
    AuthorisationRequestKind,
    AuthorisationRequestStatus,
    AwardStatus,
    CertificationAward,
    CertificationLevel,
    EquipmentTrainer,
    SupervisedSession,
    SupervisionStatus,
    TrainingAuditLog,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, make_slots, make_user
from .test_authorisation_gate import _no_portal_lock, book, code, gate_on, make_booking  # noqa: F401
from .test_operator_duty import PASS_ITEMS

pytestmark = pytest.mark.django_db
AUTH = f"{API}/authorisation"
GATE = authorisation.GATE_CODE
SUP = "SUPERVISION_REQUIRED"


@pytest.fixture(autouse=True)
def _quiet_notifications():
    with mock.patch("iic_booking.training.notify.send"):
        yield


def two_level_on(eq, **kwargs):
    return gate_on(eq, competency_levels=True, **kwargs)


def award(user, eq, code_, *, status=AwardStatus.ACTIVE, valid_until=None):
    now = timezone.now()
    return CertificationAward.objects.create(
        user=user, equipment=eq, level=CertificationLevel.objects.get(code=code_), status=status,
        awarded_at=now - timedelta(days=10),
        valid_until=valid_until if valid_until is not None else now + relativedelta(months=12),
    )


def induct(world):
    """Training request -> approved by Lab staff = Beginner."""
    resp = client_for(world.student).post(f"{AUTH}/requests/", {"equipment_id": world.equipment.pk}, format="json")
    assert resp.status_code == 201, resp.data
    resp = client_for(world.operator).post(f"{AUTH}/requests/{resp.data['id']}/approve/", {"note": "Induction done"}, format="json")
    assert resp.status_code == 200, resp.data
    return resp


def assess(actor, world, **extra):
    body = {"equipment_id": world.equipment.equipment_id, "user_id": world.student.id, "theory_score_pct": 85,
            "practical_items": PASS_ITEMS, **extra}
    return client_for(actor).post(f"{API}/assessments/", body, format="json")


# --------------------------------------------------------------------------- levels and off switch
def test_levels_are_seeded_with_supervised_and_independent_ranks(db):
    beginner = CertificationLevel.objects.get(code="BEGINNER")
    expert = CertificationLevel.objects.get(code="EXPERT")
    assert beginner.rank < competency.INDEPENDENT_RANK <= expert.rank


def test_two_level_off_keeps_the_single_step_flow(world):
    gate_on(world.equipment)
    resp = induct(world)
    assert CertificationAward.objects.get(pk=resp.data["award_id"]).level.code == "TRAINED"
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["status"] == "AUTHORISED" and st["two_level"] is False
    assert code(book(world.student, world.equipment)) not in (GATE, SUP)


# --------------------------------------------------------------------------- induction -> Beginner
def test_induction_makes_a_beginner_who_needs_a_supervisor(world):
    two_level_on(world.equipment)
    resp = induct(world)
    assert CertificationAward.objects.get(pk=resp.data["award_id"]).level.code == "BEGINNER"
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["stage"] == "BEGINNER" and st["supervised_only"] and not st["allowed"]
    assert st["can_request_assessment"] and not st["can_request"]
    assert {p["id"] for p in st["supervisors"]} >= {world.operator.id, world.oic.id}
    assert world.student.id not in {p["id"] for p in st["supervisors"]}

    resp = book(world.student, world.equipment)
    assert resp.status_code == 403 and code(resp) == SUP and "supervisor" in resp.data["error"].lower()
    assert TrainingAuditLog.objects.filter(action="competency.beginner_granted").exists()


def test_beginner_with_eligible_supervisor_passes_gate_ineligible_or_self_do_not(world):
    two_level_on(world.equipment)
    award(world.student, world.equipment, "BEGINNER")
    eq = world.equipment
    assert authorisation.check_booking(world.student, eq, actor=world.student, supervisor=world.operator) is None
    outsider = make_user(user_type=UserType.STUDENT, name="Someone Else")
    assert authorisation.check_booking(world.student, eq, actor=world.student, supervisor=outsider)["code"] == SUP
    assert authorisation.check_booking(world.student, eq, actor=world.student, supervisor=world.student)["code"] == SUP
    assert authorisation.check_booking(world.student, eq, actor=world.student)["code"] == SUP
    # Through the API the supervisor id is read from the body.
    assert code(book(world.student, eq, supervisor_id=outsider.id)) == SUP
    assert code(book(world.student, eq, supervisor_id=world.operator.id)) not in (GATE, SUP)


def test_supervised_bookings_switched_off_blocks_beginners(world):
    two_level_on(world.equipment, beginner_supervised_allowed=False)
    award(world.student, world.equipment, "BEGINNER")
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["stage"] == "BEGINNER" and not st["supervised_only"] and st["supervisors"] == []
    assert code(book(world.student, world.equipment, supervisor_id=world.operator.id)) == GATE


def test_supervised_session_is_created_confirmed_and_recorded(world):
    two_level_on(world.equipment)
    award(world.student, world.equipment, "BEGINNER")
    b = make_booking(world.student, world.equipment)
    slot = make_slots(world.equipment, day=timezone.localdate() + timedelta(days=3), hours=[10])[0]
    DailySlot.objects.filter(pk=slot.pk).update(booking=b, status="BOOKED")
    session = competency.attach_supervision(b, world.operator, actor=world.student)
    assert session.status == SupervisionStatus.REQUESTED and session.supervisor == world.operator
    assert competency.booking_supervisor(b) == world.operator
    # Rescheduling keeps the supervisor already on the booking.
    assert authorisation.check_booking(world.student, world.equipment, actor=world.student, booking=b) is None

    stranger = make_user(user_type=UserType.STUDENT, name="Not Staff")
    assert client_for(stranger).post(f"{AUTH}/supervision/{session.pk}/confirm/", {}, format="json").status_code == 403
    assert client_for(world.operator).post(f"{AUTH}/supervision/{session.pk}/decline/", {}, format="json").status_code == 400
    inbox = client_for(world.operator).get(f"{AUTH}/supervision/", {"scope": "supervising"})
    assert [r["id"] for r in inbox.data["results"]] == [session.pk] and inbox.data["results"][0]["can_respond"]
    resp = client_for(world.operator).post(f"{AUTH}/supervision/{session.pk}/confirm/", {"note": "See you there"}, format="json")
    assert resp.status_code == 200 and resp.data["status"] == "CONFIRMED"
    # Not yet started: cannot be recorded.
    assert client_for(world.operator).post(f"{AUTH}/supervision/{session.pk}/attended/", {}, format="json").status_code == 400
    DailySlot.objects.filter(pk=slot.pk).update(start_datetime=timezone.now() - timedelta(hours=1),
                                                end_datetime=timezone.now())
    resp = client_for(world.operator).post(f"{AUTH}/supervision/{session.pk}/attended/", {"remarks": "Good handling"}, format="json")
    assert resp.status_code == 200 and resp.data["status"] == "ATTENDED"
    mine = client_for(world.student).get(f"{AUTH}/supervision/")
    assert [r["status"] for r in mine.data["results"]] == ["ATTENDED"]
    actions = set(TrainingAuditLog.objects.values_list("action", flat=True))
    assert {"competency.supervised_booking", "competency.supervision_confirm", "competency.supervision_attended"} <= actions


def test_cancelling_the_booking_cancels_the_session(world):
    two_level_on(world.equipment)
    award(world.student, world.equipment, "BEGINNER")
    b = make_booking(world.student, world.equipment)
    session = competency.attach_supervision(b, world.oic, actor=world.student)
    b.status = BookingStatus.CANCELLED
    b.save()
    session.refresh_from_db()
    assert session.status == SupervisionStatus.CANCELLED


def test_supervision_is_not_attached_for_an_expert_or_on_single_step_equipment(world):
    gate_on(world.equipment)
    b = make_booking(world.student, world.equipment)
    assert competency.attach_supervision(b, world.operator, actor=world.student) is None
    EquipmentTrainer.objects.all().delete()
    world.equipment.authorisation_setting.competency_levels = True
    world.equipment.authorisation_setting.save()
    award(world.student, world.equipment, "EXPERT")
    assert competency.attach_supervision(b, world.operator, actor=world.student) is None
    assert not SupervisedSession.objects.exists()


# --------------------------------------------------------------------------- trainers
def test_trainer_designation_needs_expert_and_oic_and_removal_needs_reason(world):
    two_level_on(world.equipment)
    expert = make_user(user_type=UserType.STUDENT, department=world.dept, name="Senior Scholar")
    url = f"{AUTH}/equipment/{world.equipment.pk}/trainers/"
    assert client_for(world.oic).post(url, {"user_id": expert.id}, format="json").status_code == 400  # not Expert yet
    award(expert, world.equipment, "EXPERT")
    assert client_for(world.student).post(url, {"user_id": expert.id}, format="json").status_code == 403
    resp = client_for(world.oic).post(url, {"user_id": expert.id, "note": "PhD year 4"}, format="json")
    assert resp.status_code == 201, resp.data
    assert client_for(world.oic).post(url, {"user_id": expert.id}, format="json").status_code == 409
    assert competency.is_eligible_supervisor(expert, world.equipment, student=world.student)
    assert certification.can_assess(expert, world.equipment.equipment_id)
    listing = client_for(world.operator).get(url)
    assert [r["id"] for r in listing.data["results"]] == [resp.data["id"]]

    remove = f"{AUTH}/trainers/{resp.data['id']}/remove/"
    assert client_for(world.oic).post(remove, {}, format="json").status_code == 400
    assert client_for(world.oic).post(remove, {"reason": "Graduated"}, format="json").status_code == 200
    assert not competency.is_eligible_supervisor(expert, world.equipment, student=world.student)
    assert EquipmentTrainer.objects.filter(user=expert, is_active=False, remove_reason="Graduated").exists()


def test_trainer_whose_expert_authorisation_expires_stops_being_eligible(world):
    two_level_on(world.equipment)
    expert = make_user(user_type=UserType.STUDENT, department=world.dept, name="Senior Scholar")
    a = award(expert, world.equipment, "EXPERT")
    competency.designate_trainer(world.equipment, world.oic, expert)
    assert competency.is_eligible_supervisor(expert, world.equipment, student=world.student)
    CertificationAward.objects.filter(pk=a.pk).update(valid_until=timezone.now() - timedelta(days=1))
    assert not competency.is_eligible_supervisor(expert, world.equipment, student=world.student)


# --------------------------------------------------------------------------- Expert assessment
def test_assessment_request_needs_a_beginner(world):
    two_level_on(world.equipment)
    url = f"{AUTH}/assessment-requests/"
    resp = client_for(world.student).post(url, {"equipment_id": world.equipment.pk}, format="json")
    assert resp.status_code == 400 and resp.data["code"] == "beginner_required"
    award(world.student, world.equipment, "BEGINNER")
    resp = client_for(world.student).post(url, {"equipment_id": world.equipment.pk, "message": "Ready"}, format="json")
    assert resp.status_code == 201 and resp.data["kind"] == "ASSESSMENT"
    assert client_for(world.student).post(url, {"equipment_id": world.equipment.pk}, format="json").status_code == 409
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["stage"] == "ASSESSMENT_PENDING" and not st["can_request_assessment"] and st["supervised_only"]
    # The assessment request is decided by recording the assessment, not approved directly.
    approve = client_for(world.oic).post(f"{AUTH}/requests/{resp.data['id']}/approve/", {}, format="json")
    assert approve.status_code == 400


def test_trainer_pass_waits_for_oic_approval_then_expert(world):
    two_level_on(world.equipment)
    award(world.student, world.equipment, "BEGINNER")
    req = competency.request_assessment(world.student, world.equipment)
    resp = assess(world.operator, world)
    assert resp.status_code == 201, resp.data
    assert resp.data["result"] == "PASS" and not resp.data["award_id"] and resp.data["awaiting_sign_off"]
    req.refresh_from_db()
    assert req.status in (AuthorisationRequestStatus.PENDING, AuthorisationRequestStatus.SCHEDULED)
    assert code(book(world.student, world.equipment)) == SUP

    sign = f"{API}/assessments/{resp.data['id']}/sign-off/"
    assert client_for(world.operator).post(sign).status_code == 403
    assert client_for(world.oic).post(sign).status_code in (200, 201)
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["stage"] == "EXPERT" and st["allowed"] and not st["supervised_only"]
    req.refresh_from_db()
    assert req.status == AuthorisationRequestStatus.APPROVED and req.award_id
    assert code(book(world.student, world.equipment)) not in (GATE, SUP)


def test_oic_pass_is_immediate_and_waiver_lets_a_trainer_pass_stand(world):
    two_level_on(world.equipment)
    award(world.student, world.equipment, "BEGINNER")
    resp = assess(world.oic, world)
    assert resp.status_code == 201 and resp.data["award_id"]
    assert CertificationAward.objects.get(pk=resp.data["award_id"]).level.code == "EXPERT"

    other = make_user(user_type=UserType.STUDENT, department=world.dept, name="Student Two")
    world.equipment.authorisation_setting.expert_requires_oic_approval = False
    world.equipment.authorisation_setting.save()
    award(other, world.equipment, "BEGINNER")
    resp = assess(world.operator, world, user_id=other.id)
    assert resp.status_code == 201 and resp.data["award_id"]
    log = TrainingAuditLog.objects.filter(action="assessment.signed_off").order_by("-id").first()
    assert log.after.get("approval_waived") is True


def test_failed_assessment_closes_the_request_and_keeps_the_beginner(world):
    two_level_on(world.equipment)
    award(world.student, world.equipment, "BEGINNER")
    req = competency.request_assessment(world.student, world.equipment)
    items = [{**i, "passed": i["key"] != "safety"} for i in PASS_ITEMS]
    resp = assess(world.operator, world, practical_items=items)
    assert resp.status_code == 201 and resp.data["result"] in ("RETAKE", "FAIL")
    req.refresh_from_db()
    assert req.status == AuthorisationRequestStatus.REJECTED
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["stage"] == "BEGINNER" and st["can_request_assessment"]


def test_nobody_approves_their_own_assessment(world):
    two_level_on(world.equipment)
    award(world.oic, world.equipment, "BEGINNER")
    # The OIC cannot assess themselves at all, nor sign off an assessment about themselves.
    assert assess(world.oic, world, user_id=world.oic.id).status_code in (400, 403)
    a = assess(world.operator, world, user_id=world.oic.id)
    assert a.status_code == 201 and not a.data["award_id"]
    assert client_for(world.oic).post(f"{API}/assessments/{a.data['id']}/sign-off/").status_code == 403


def test_expired_or_revoked_expert_cannot_book(world):
    two_level_on(world.equipment)
    a = award(world.student, world.equipment, "EXPERT")
    assert authorisation.evaluate(world.student, world.equipment)["stage"] == "EXPERT"
    CertificationAward.objects.filter(pk=a.pk).update(valid_until=timezone.now() - timedelta(days=1))
    st = authorisation.evaluate(world.student, world.equipment)
    assert st["stage"] == "EXPIRED" and not st["allowed"]
    assert code(book(world.student, world.equipment)) == GATE
    CertificationAward.objects.filter(pk=a.pk).update(status=AwardStatus.REVOKED, valid_until=timezone.now() + timedelta(days=90))
    assert authorisation.evaluate(world.student, world.equipment)["stage"] == "REVOKED"
    assert code(book(world.student, world.equipment)) == GATE


def test_settings_switches_round_trip(world):
    two_level_on(world.equipment)
    url = f"{AUTH}/equipment/{world.equipment.pk}/"
    resp = client_for(world.oic).get(url)
    assert resp.status_code == 200 and resp.data["competency_levels"] is True
    assert resp.data["expert_requires_oic_approval"] is True and resp.data["beginner_supervised_allowed"] is True
    resp = client_for(world.oic).put(url, {"required": True, "competency_levels": True, "expert_requires_oic_approval": False},
                                     format="json")
    assert resp.status_code == 200, resp.data
    world.equipment.authorisation_setting.refresh_from_db()
    assert world.equipment.authorisation_setting.expert_requires_oic_approval is False
    assert client_for(world.student).put(url, {"competency_levels": False}, format="json").status_code == 403
