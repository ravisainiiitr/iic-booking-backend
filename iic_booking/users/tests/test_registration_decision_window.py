"""Faculty decision by email (Approve / Decline), the 24-hour window, timeouts and the sign-up form changes."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from urllib.parse import unquote

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users import registration_approvals as svc
from iic_booking.users.models import (
    Department,
    DepartmentType,
    RegistrationApproval,
    RegistrationApprovalEvent,
    RegistrationApprovalStatus,
    User,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

DECISION_URL = "/api/registration-approvals/email-decision/"
REGISTER_URL = "/api/auth/register/"
A = RegistrationApprovalEvent.Action


@pytest.fixture(autouse=True)
def _frontend(settings):
    settings.FRONTEND_URL = "https://equip.example.test"


@pytest.fixture
def dept():
    return Department.objects.create(name="Chemistry", code="CY", department_type=DepartmentType.INTERNAL)


@pytest.fixture
def faculty(dept):
    return UserFactory(
        email="prof.sharma@iitr.ac.in", name="Rakesh Sharma", user_type=UserType.FACULTY, admin_approved=True, department=dept
    )


@pytest.fixture
def admin():
    return UserFactory(email="main.admin@iitr.ac.in", name="Main Admin", user_type=UserType.ADMIN, admin_approved=True)


def _postdoc(faculty, **kw):
    today = timezone.localdate()
    fields = {
        "email": f"pd.{uuid.uuid4().hex[:6]}@gmail.com",
        "name": "Asha Verma",
        "user_type": UserType.STUDENT,
        "user_type_alias": "IITR Post Doctoral Fellows",
        "supervisor": faculty,
        "email_verified": True,
        "admin_approved": False,
        "supervisor_approved": False,
        "program_end_date": today + timedelta(days=200),
    }
    fields.update(kw)
    return UserFactory(**fields)


def _token_from_mail(action: str = "approve") -> str:
    faculty_mail = next(m for m in mail.outbox if "/registration-decision?token=" in m.body)
    match = re.search(r"/registration-decision\?token=([^&\s]+)&action=" + action, faculty_mail.body)
    assert match, faculty_mail.body
    return unquote(match.group(1))


def _approve(raw: str, **extra):
    return {
        "token": raw,
        "decision": "approve",
        "disclaimer_accepted": True,
        "disclaimer_version": svc.DISCLAIMER_VERSION,
        **extra,
    }


def _subject_actions(email: str) -> list[str]:
    return list(
        RegistrationApprovalEvent.objects.filter(subject_email=email).order_by("id").values_list("action", flat=True)
    )


# --- forwarding starts the window ------------------------------------------------------------------


def test_forward_sets_deadline_and_emails_both_sides(faculty):
    before = timezone.now()
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)

    assert approval.status == RegistrationApprovalStatus.PENDING_FACULTY
    assert before + timedelta(hours=24) <= approval.decision_deadline <= timezone.now() + timedelta(hours=24)
    token = approval.tokens.get(used_at__isnull=True)
    assert token.expires_at == approval.decision_deadline

    faculty_mail, user_mail = mail.outbox
    assert faculty_mail.to == [faculty.email]
    assert "action=approve" in faculty_mail.body and "action=decline" in faculty_mail.body
    assert "Please respond within 24 hours, by" in faculty_mail.body and "IST" in faculty_mail.body
    assert "Approve" in faculty_mail.alternatives[0][0] and "Decline" in faculty_mail.alternatives[0][0]
    assert user_mail.to == [user.email]
    assert "24 hours" in user_mail.body and "Rakesh Sharma" in user_mail.body
    assert "registration-decision" not in user_mail.body
    assert A.USER_NOTIFIED in _subject_actions(user.email)


def test_reminder_keeps_deadline_and_reforward_restarts_it(faculty, admin):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    first_deadline = approval.decision_deadline

    svc.remind(user, actor=admin)
    approval.refresh_from_db()
    assert approval.decision_deadline == first_deadline

    RegistrationApproval.objects.filter(pk=approval.pk).update(decision_deadline=timezone.now() + timedelta(hours=1))
    svc.forward(user, actor=admin)
    approval.refresh_from_db()
    assert approval.decision_deadline > timezone.now() + timedelta(hours=23)


def test_self_verify_response_mentions_the_window(faculty):
    from django.contrib.auth.tokens import default_token_generator
    from django.utils.encoding import force_bytes
    from django.utils.http import urlsafe_base64_encode

    user = _postdoc(faculty, email_verified=False, email="new.pd@gmail.com")
    User.objects.filter(pk=user.pk).update(verification_email_sent_at=timezone.now())
    user.refresh_from_db()
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    res = APIClient().post(f"/api/auth/self-verify/{uid}/{token}/", {"action": "accept"}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["pending_faculty"] is True and res.data["decision_window_hours"] == 24
    assert res.data["decision_deadline"] and "24 hours" in res.data["message"]


# --- decision from the email buttons -------------------------------------------------------------------


def test_email_approve_needs_confirmation_and_works_without_sign_in(faculty):
    user = _postdoc(faculty)
    svc.on_registration_verified(user)
    raw = _token_from_mail("approve")
    client = APIClient()

    res = client.get(DECISION_URL, {"token": raw})
    assert res.status_code == 200, res.data
    item = res.data["item"]
    assert item["user"]["name"] == "Asha Verma" and "Rakesh Sharma" in item["faculty_name"]
    assert item["decision_deadline"] and item["window_hours"] == 24
    assert item["disclaimer_version"] == svc.DISCLAIMER_VERSION

    res = client.post(DECISION_URL, {"token": raw, "decision": "approve"}, format="json")
    assert res.status_code == 400 and res.data["code"] == "disclaimer_required"

    res = client.post(DECISION_URL, _approve(raw), format="json")
    assert res.status_code == 200, res.data
    assert res.data["decision"] == "approved"
    user.refresh_from_db()
    assert user.admin_approved and user.supervisor_approved and user.is_active
    event = RegistrationApprovalEvent.objects.get(user=user, action=A.APPROVED)
    assert event.channel == "email_link" and event.actor == faculty

    res = client.post(DECISION_URL, _approve(raw), format="json")
    assert res.status_code == 410 and res.data["code"] == "already_decided"


def test_email_decline_needs_reason_removes_account_and_keeps_audit(faculty):
    user = _postdoc(faculty, emp_id="PD-77", phone_number="9876543210")
    email = user.email
    svc.on_registration_verified(user)
    raw = _token_from_mail("decline")
    client = APIClient()

    res = client.post(DECISION_URL, {"token": raw, "decision": "decline"}, format="json")
    assert res.status_code == 400 and res.data["code"] == "reason_required"

    res = client.post(DECISION_URL, {"token": raw, "decision": "decline", "reason": "Not in my group"}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["decision"] == "declined" and res.data["account_removed"] is True
    assert not User.objects.filter(email=email).exists()

    declined = mail.outbox[-1]
    assert declined.to == [email] and faculty.email in (declined.cc or [])
    assert "Not in my group" in declined.body and "/auth?mode=register" in declined.body

    actions = _subject_actions(email)
    assert actions[-2:] == [A.DISAPPROVED, A.ACCOUNT_REMOVED]
    disapproved = RegistrationApprovalEvent.objects.get(subject_email=email, action=A.DISAPPROVED)
    snap = disapproved.details["snapshot"]
    assert snap["employee_id"] == "PD-77" and snap["faculty_email"] == faculty.email and snap["programme_validity"]
    removed = RegistrationApprovalEvent.objects.get(subject_email=email, action=A.ACCOUNT_REMOVED)
    assert removed.details["removed"] is True and removed.user_id is None

    res = client.get(DECISION_URL, {"token": raw})
    assert res.status_code == 410 and res.data["code"] == "already_decided"


def test_decline_keeps_an_account_that_has_signed_in(faculty):
    user = _postdoc(faculty, last_login=timezone.now())
    svc.on_registration_verified(user)
    raw = _token_from_mail("decline")
    res = APIClient().post(DECISION_URL, {"token": raw, "decision": "decline", "reason": "Wrong person"}, format="json")
    assert res.status_code == 200 and res.data["account_removed"] is False
    assert RegistrationApproval.objects.get(user=user).status == RegistrationApprovalStatus.REJECTED
    kept = RegistrationApprovalEvent.objects.get(user=user, action=A.ACCOUNT_REMOVED)
    assert kept.details["removed"] is False and kept.details["kept_reason"] == "has_signed_in"


def test_signed_in_as_someone_else_is_refused(faculty, admin):
    user = _postdoc(faculty)
    svc.on_registration_verified(user)
    raw = _token_from_mail("approve")
    client = APIClient()
    client.force_authenticate(admin)
    res = client.get(DECISION_URL, {"token": raw})
    assert res.status_code == 403 and res.data["code"] == "wrong_faculty"
    assert RegistrationApprovalEvent.objects.filter(action=A.TOKEN_REFUSED, actor=admin).exists()


def test_invalid_token_is_404():
    res = APIClient().get(DECISION_URL, {"token": "nonsense"})
    assert res.status_code == 404 and res.data["code"] == "token_invalid"


# --- the 24-hour timeout --------------------------------------------------------------------------------


def _overdue(approval):
    RegistrationApproval.objects.filter(pk=approval.pk).update(decision_deadline=timezone.now() - timedelta(minutes=1))


def test_timeout_task_declines_removes_and_tells_the_user(faculty):
    user = _postdoc(faculty)
    email = user.email
    approval = svc.on_registration_verified(user)
    raw = _token_from_mail("approve")
    _overdue(approval)
    mail.outbox.clear()

    from iic_booking.users.tasks import registration_decision_timeouts

    assert registration_decision_timeouts() == {"timed_out": 1, "skipped": 0}
    assert not User.objects.filter(email=email).exists()
    assert [m.to for m in mail.outbox] == [[email]]
    assert "timed out" in mail.outbox[0].subject.lower() and "/auth?mode=register" in mail.outbox[0].body
    assert _subject_actions(email)[-2:] == [A.TIMED_OUT, A.ACCOUNT_REMOVED]

    res = APIClient().post(DECISION_URL, _approve(raw), format="json")
    assert res.status_code == 410 and res.data["code"] == "timed_out"
    assert "Request timed out" in res.data["error"]
    assert registration_decision_timeouts() == {"timed_out": 0, "skipped": 0}


def test_late_click_times_out_at_once_even_before_the_task_runs(faculty):
    user = _postdoc(faculty)
    email = user.email
    approval = svc.on_registration_verified(user)
    raw = _token_from_mail("approve")
    _overdue(approval)

    res = APIClient().get(DECISION_URL, {"token": raw})
    assert res.status_code == 410 and res.data["code"] == "timed_out"
    assert not User.objects.filter(email=email).exists()
    assert "timed out" in mail.outbox[-1].subject.lower()


def test_portal_decision_after_deadline_is_refused(faculty):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    _overdue(approval)
    client = APIClient()
    client.force_authenticate(faculty)
    res = client.post(
        f"/api/registration-approvals/{approval.pk}/decide/",
        {"decision": "approve", "disclaimer_accepted": True, "disclaimer_version": svc.DISCLAIMER_VERSION},
        format="json",
    )
    assert res.status_code == 410 and res.data["code"] == "timed_out"
    assert not User.objects.filter(pk=user.pk).exists()


def test_requests_never_sent_with_a_deadline_are_never_timed_out(faculty):
    """Existing pending requests (never forwarded, or forwarded before the window existed) have no deadline."""
    never_forwarded = _postdoc(faculty)
    svc.get_or_create_approval(never_forwarded)
    legacy_forwarded = _postdoc(faculty)
    legacy = svc.on_registration_verified(legacy_forwarded)
    RegistrationApproval.objects.filter(pk=legacy.pk).update(decision_deadline=None)
    no_row = _postdoc(faculty)

    far_future = timezone.now() + timedelta(days=365)
    assert svc.process_decision_timeouts(now=far_future) == {"timed_out": 0, "skipped": 0}
    for user in (never_forwarded, legacy_forwarded, no_row):
        assert User.objects.filter(pk=user.pk).exists()
    assert RegistrationApproval.objects.get(user=never_forwarded).status == RegistrationApprovalStatus.PENDING_ADMIN
    assert RegistrationApproval.objects.get(user=legacy_forwarded).status == RegistrationApprovalStatus.PENDING_FACULTY
    report = svc.production_report()
    assert report["with_faculty_no_timer"] == 1 and report["with_faculty_overdue"] == 0


def test_admin_can_still_approve_and_list_shows_deadline(faculty, admin):
    user = _postdoc(faculty)
    svc.on_registration_verified(user)
    client = APIClient()
    client.force_authenticate(admin)
    rows = client.get("/api/admin/registration-requests/", {"status": "pending_faculty"}).data["results"]
    assert rows[0]["decision_deadline"]
    res = client.post(f"/api/admin/registration-requests/{user.pk}/approve/", {}, format="json")
    assert res.status_code == 200
    user.refresh_from_db()
    assert user.admin_approved
    assert svc.process_decision_timeouts(now=timezone.now() + timedelta(days=2))["timed_out"] == 0


# --- sign-up form ---------------------------------------------------------------------------------------


def _register_payload(**extra):
    today = timezone.localdate()
    data = {
        "email": f"new.{uuid.uuid4().hex[:6]}@gmail.com",
        "password": "Str0ng-pass!",
        "password_confirm": "Str0ng-pass!",
        "name": "Neha Rao",
        "gender": "female",
        "phone_number": "9876543210",
        "program_end_date": (today + timedelta(days=200)).isoformat(),
    }
    data.update(extra)
    return data


def test_user_types_offer_one_iitr_startup():
    res = APIClient().get("/api/auth/register/user-types/")
    assert res.status_code == 200
    names = [t["name"] for t in res.data["user_types"]]
    assert "IITR Startup" in names
    assert "IITR Startups" not in names and "Startup Incubated at IIT Roorkee" not in names
    iitr = {t["name"] for t in res.data["user_types"] if t.get("iitr")}
    assert iitr == {"IITR Startup", "IITR Post Doctoral Fellows", "IITR Research Associates in Projects"}


def test_iitr_startup_needs_faculty_and_internal_department_profile_picture_optional(faculty, dept):
    client = APIClient()
    res = client.post(REGISTER_URL, _register_payload(user_type=UserType.STARTUP_INCUBATED_IITR, department=dept.pk))
    assert res.status_code == 400 and "supervisor" in str(res.data)

    res = client.post(REGISTER_URL, _register_payload(user_type=UserType.STARTUP_INCUBATED_IITR, supervisor=faculty.pk))
    assert res.status_code == 400 and res.data["fieldErrors"]["department"]

    external = Department.objects.create(name="Acme", code="ACME", department_type=DepartmentType.EXTERNAL)
    res = client.post(
        REGISTER_URL,
        _register_payload(user_type=UserType.STARTUP_INCUBATED_IITR, supervisor=faculty.pk, department=external.pk),
    )
    assert res.status_code == 400

    payload = _register_payload(user_type=UserType.STARTUP_INCUBATED_IITR, supervisor=faculty.pk, department=dept.pk)
    res = client.post(REGISTER_URL, payload)
    assert res.status_code == 201, res.data
    user = User.objects.get(email=payload["email"])
    assert user.supervisor == faculty and user.department == dept and not user.profile_picture


def test_legacy_iitr_startups_alias_registers_as_iitr_startup(faculty, dept):
    payload = _register_payload(
        user_type=UserType.INDIVIDUAL_STUDENT, user_type_alias="IITR Startups", supervisor=faculty.pk, department=dept.pk
    )
    res = APIClient().post(REGISTER_URL, payload)
    assert res.status_code == 201, res.data
    user = User.objects.get(email=payload["email"])
    assert user.user_type == UserType.STARTUP_INCUBATED_IITR and not user.user_type_alias


def test_postdoc_without_department_is_refused(faculty):
    res = APIClient().post(
        REGISTER_URL,
        _register_payload(user_type=UserType.STUDENT, user_type_alias="IITR Post Doctoral Fellows", supervisor=faculty.pk),
    )
    assert res.status_code == 400 and res.data["fieldErrors"]["department"]
