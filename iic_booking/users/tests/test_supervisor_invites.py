"""Students invite a supervisor who is not on the portal yet; the invite becomes a link request on sign-in."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication.models import CommunicationLog, CommunicationTemplate
from iic_booking.users import supervisor_invites as svc
from iic_booking.users.models import (
    Department,
    DepartmentType,
    SupervisorInvite,
    SupervisorInviteEvent,
    SupervisorInviteStatus,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/wallet/supervisor-invites/"
PROF = "prof.sharma@ch.iitr.ac.in"


@pytest.fixture(autouse=True)
def _frontend(settings):
    settings.FRONTEND_URL = "https://equip.example.test"
    settings.SUPERVISOR_INVITE_EMAIL_DOMAINS = ["iitr.ac.in"]
    settings.SUPERVISOR_INVITE_EMAIL_DAILY_CAP = 5


@pytest.fixture
def dept():
    return Department.objects.create(name="Chemistry", code="CY", department_type=DepartmentType.INTERNAL)


@pytest.fixture
def student(dept):
    return UserFactory(
        email="asha.student@iitr.ac.in",
        name="Asha Verma",
        user_type=UserType.STUDENT,
        admin_approved=True,
        department=dept,
        degree_name="PhD",
    )


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def _invite(client, email=PROF, **extra):
    return client.post(URL, {"email": email, **extra}, format="json")


def _faculty(email=PROF, **kw):
    kw.setdefault("admin_approved", True)
    return UserFactory(email=email, name="Rakesh Sharma", user_type=UserType.FACULTY, **kw)


# --- validation ---------------------------------------------------------------------------


@pytest.mark.parametrize("email", ["prof@gmail.com", "prof@iitr.ac.in.evil.com", "prof@xiitr.ac.in", "not-an-email"])
def test_rejects_non_institute_or_invalid_emails(student, email):
    res = _invite(_client(student), email)
    assert res.status_code == 400
    assert res.data["code"] in {"email_domain", "email_invalid"}
    assert not SupervisorInvite.objects.exists()
    assert len(mail.outbox) == 0


def test_accepts_institute_domain_and_department_subdomain(student):
    assert _invite(_client(student), "Prof.Kumar@IITR.ac.in").status_code == 201
    assert _invite(_client(student), PROF).status_code == 201
    assert set(SupervisorInvite.objects.values_list("email", flat=True)) == {"prof.kumar@iitr.ac.in", PROF}


def test_extra_domains_come_from_settings(student, settings):
    settings.SUPERVISOR_INVITE_EMAIL_DOMAINS = ["iitr.ac.in", "iitr.res.in"]
    assert _invite(_client(student), "pi@iitr.res.in").status_code == 201


def test_refuses_self_invite(student):
    res = _invite(_client(student), "ASHA.student@iitr.ac.in")
    assert res.status_code == 400 and res.data["code"] == "self_invite"


def test_refuses_existing_student_email_with_clear_message(student):
    UserFactory(email="ravi.student@iitr.ac.in", user_type=UserType.STUDENT, admin_approved=True)
    res = _invite(_client(student), "ravi.student@iitr.ac.in")
    assert res.status_code == 400
    assert res.data["code"] == "not_faculty"
    assert "not a faculty member" in res.data["error"]
    assert SupervisorInviteEvent.objects.filter(action="refused", email="ravi.student@iitr.ac.in").exists()


def test_points_to_search_when_faculty_is_already_on_portal(student):
    _faculty()
    res = _invite(_client(student))
    assert res.status_code == 409
    assert res.data["code"] == "faculty_on_portal"
    assert "already on the portal" in res.data["error"]
    assert set(res.data) == {"error", "code"}
    assert not SupervisorInvite.objects.exists()


def test_inactive_faculty_account_can_be_invited(student):
    _faculty(admin_approved=False)
    assert _invite(_client(student)).status_code == 201


def test_only_students_and_other_users_can_invite():
    faculty = _faculty("someone@iitr.ac.in")
    res = _invite(_client(faculty), "other.prof@iitr.ac.in")
    assert res.status_code == 403
    assert _client(faculty).get(URL).status_code == 403


def test_unauthenticated_is_rejected():
    assert APIClient().post(URL, {"email": PROF}, format="json").status_code in (401, 403)


def test_invalid_department_is_rejected(student):
    external = Department.objects.create(name="Acme Labs", code="ACME", department_type=DepartmentType.EXTERNAL)
    res = _invite(_client(student), department_id=external.id)
    assert res.status_code == 400 and res.data["code"] == "department_invalid"


# --- creation, email, token -----------------------------------------------------------------


def test_creates_invite_with_hashed_token_and_sends_templated_email(student, dept):
    res = _invite(
        _client(student),
        supervisor_name="Prof. Rakesh Sharma",
        department_id=dept.id,
        message="Please link me <b>soon</b>",
    )
    assert res.status_code == 201
    invite = SupervisorInvite.objects.get()
    assert invite.status == SupervisorInviteStatus.PENDING
    assert invite.department == dept
    assert invite.send_count == 1 and invite.last_sent_at is not None
    assert timedelta(days=29) < invite.expires_at - timezone.now() <= timedelta(days=30)

    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.to == [PROF]
    assert "Asha Verma" in msg.subject
    body = msg.body
    assert "Hello Prof. Rakesh Sharma" in body
    assert "PhD" in body and "Chemistry" in body
    assert "Sign in to review" in body
    html = msg.alternatives[0][0]
    assert "Sign in to review" in html
    assert "&lt;b&gt;soon&lt;/b&gt;" in html and "<b>soon</b>" not in html

    # The raw token is only in the email link; the database keeps its SHA-256 digest.
    link = next(part for part in body.split() if "supervisor_invite" in part)
    assert link.startswith("https://equip.example.test/login?next=")
    from urllib.parse import parse_qs, unquote, urlparse

    target = unquote(parse_qs(urlparse(link).query)["next"][0])
    token = parse_qs(urlparse(target).query)["supervisor_invite"][0].split("#")[0]
    assert len(token) >= 40
    assert invite.token_hash == svc.hash_token(token)
    assert token not in invite.token_hash
    assert not SupervisorInvite.objects.filter(token_hash=token).exists()

    template = CommunicationTemplate.objects.get(code="supervisor_invite_email")
    log = CommunicationLog.objects.get(template=template)
    assert log.recipient is None and log.recipient_email == PROF and log.status == "sent"
    assert invite.token_hash not in log.message
    assert SupervisorInviteEvent.objects.filter(invite=invite, action="created", actor=student).exists()
    assert "token" not in res.data["invite"]


def test_existing_admin_edited_template_is_used_and_not_overwritten(student):
    from iic_booking.communication.utils import clear_current_user

    clear_current_user()
    CommunicationTemplate.objects.create(
        code="supervisor_invite_email",
        name="Supervisor Invite Email (custom)",
        communication_type="email",
        subject="Custom subject for {{ student_name }}",
        body_text="Custom body {{ link }}",
        body_html="<p>Custom {{ link }}</p>",
    )
    assert _invite(_client(student)).status_code == 201
    assert mail.outbox[0].subject == "Custom subject for Asha Verma"
    tpl = CommunicationTemplate.objects.get(code="supervisor_invite_email")
    assert tpl.subject == "Custom subject for {{ student_name }}"


def test_test_account_student_invites_are_redirected(student, settings):
    settings.TEST_ACCOUNT_EMAIL_REDIRECT = "qa.inbox@example.test"
    student.is_test_account = True
    student.save()
    assert _invite(_client(student)).status_code == 201
    assert PROF not in mail.outbox[0].to


# --- rate limits ----------------------------------------------------------------------------


def test_max_three_active_invites_per_student(student):
    client = _client(student)
    for i in range(3):
        assert _invite(client, f"prof{i}@iitr.ac.in").status_code == 201
    res = _invite(client, "prof4@iitr.ac.in")
    assert res.status_code == 429 and res.data["code"] == "too_many_active"
    cancel_id = SupervisorInvite.objects.filter(email="prof0@iitr.ac.in").get().id
    assert client.post(f"{URL}{cancel_id}/cancel/").status_code == 200
    assert _invite(client, "prof4@iitr.ac.in").status_code == 201


def test_duplicate_pending_invite_for_same_email_is_refused(student):
    client = _client(student)
    assert _invite(client).status_code == 201
    res = _invite(client, PROF.upper())
    assert res.status_code == 400 and res.data["code"] == "duplicate_invite"


def test_resend_at_most_once_per_24_hours(student):
    client = _client(student)
    _invite(client)
    invite = SupervisorInvite.objects.get()
    old_hash = invite.token_hash
    res = client.post(f"{URL}{invite.id}/resend/")
    assert res.status_code == 429 and res.data["code"] == "resend_cooldown"
    assert len(mail.outbox) == 1

    SupervisorInvite.objects.filter(pk=invite.pk).update(last_sent_at=timezone.now() - timedelta(hours=25))
    res = client.post(f"{URL}{invite.id}/resend/")
    assert res.status_code == 200
    invite.refresh_from_db()
    assert invite.send_count == 2
    assert invite.token_hash != old_hash
    assert len(mail.outbox) == 2
    assert client.post(f"{URL}{invite.id}/resend/").status_code == 429
    assert SupervisorInviteEvent.objects.filter(invite=invite, action="resent").count() == 1


def test_per_email_daily_cap_across_students(dept, settings):
    settings.SUPERVISOR_INVITE_EMAIL_DAILY_CAP = 2
    students = [
        UserFactory(email=f"s{i}@iitr.ac.in", user_type=UserType.STUDENT, admin_approved=True) for i in range(3)
    ]
    assert _invite(_client(students[0])).status_code == 201
    assert _invite(_client(students[1])).status_code == 201
    res = _invite(_client(students[2]))
    assert res.status_code == 429 and res.data["code"] == "email_daily_cap"
    assert len(mail.outbox) == 2


# --- list / cancel / permissions -----------------------------------------------------------


def test_list_shows_own_invites_with_resend_and_cancel_flags(student):
    client = _client(student)
    _invite(client)
    other = UserFactory(email="other.s@iitr.ac.in", user_type=UserType.STUDENT, admin_approved=True)
    _invite(_client(other), "someone.else@iitr.ac.in")
    data = client.get(URL).data
    assert [i["email"] for i in data["invites"]] == [PROF]
    row = data["invites"][0]
    assert row["can_cancel"] is True and row["can_resend"] is False and row["can_resend_at"]
    assert data["limits"]["max_active"] == 3 and data["limits"]["valid_days"] == 30


def test_cannot_resend_or_cancel_someone_elses_invite(student):
    other = UserFactory(email="other.s@iitr.ac.in", user_type=UserType.STUDENT, admin_approved=True)
    _invite(_client(other))
    invite = SupervisorInvite.objects.get()
    assert _client(student).post(f"{URL}{invite.id}/cancel/").status_code == 404
    assert _client(student).post(f"{URL}{invite.id}/resend/").status_code == 404
    invite.refresh_from_db()
    assert invite.status == SupervisorInviteStatus.PENDING


def test_cancel_then_cannot_resend(student):
    client = _client(student)
    _invite(client)
    invite = SupervisorInvite.objects.get()
    assert client.post(f"{URL}{invite.id}/cancel/").status_code == 200
    invite.refresh_from_db()
    assert invite.status == SupervisorInviteStatus.CANCELLED and invite.cancelled_at
    SupervisorInvite.objects.filter(pk=invite.pk).update(last_sent_at=timezone.now() - timedelta(days=2))
    res = client.post(f"{URL}{invite.id}/resend/")
    assert res.status_code == 400 and res.data["code"] == "not_pending"
    assert SupervisorInviteEvent.objects.filter(invite=invite, action="cancelled").exists()


def test_expired_invites_are_marked_and_not_converted(student):
    client = _client(student)
    _invite(client)
    SupervisorInvite.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
    rows = client.get(URL).data["invites"]
    assert rows[0]["status"] == "expired" and rows[0]["can_resend"] is False
    assert SupervisorInviteEvent.objects.filter(action="expired").count() == 1

    faculty = _faculty()
    assert svc.convert_invites_for_faculty(faculty) == []
    assert not WalletJoinRequest.objects.exists()
    # Expired invites do not count towards the active limit.
    assert _invite(client, "new.prof@iitr.ac.in").status_code == 201


# --- faculty sign-in conversion -----------------------------------------------------------


def test_faculty_sign_in_turns_pending_invites_into_pending_link_requests(student):
    other = UserFactory(email="bina@iitr.ac.in", name="Bina", user_type=UserType.STUDENT, admin_approved=True)
    _invite(_client(student), message="I am in your group")
    _invite(_client(other))
    mail.outbox.clear()

    faculty = _faculty()
    converted = svc.convert_invites_for_faculty(faculty)
    assert len(converted) == 2

    requests = WalletJoinRequest.objects.filter(faculty=faculty).order_by("pk")
    assert requests.count() == 2
    assert all(r.status == WalletJoinRequestStatus.PENDING for r in requests)
    assert all(r.wallet_id == faculty.wallet.id for r in requests)
    assert requests.get(student=student).message == "I am in your group"

    for invite in SupervisorInvite.objects.all():
        assert invite.status == SupervisorInviteStatus.ACCEPTED
        assert invite.accepted_by == faculty and invite.join_request_id
    assert SupervisorInviteEvent.objects.filter(action="accepted").count() == 2

    student_mails = [m for m in mail.outbox if student.email in m.to]
    assert len(student_mails) == 1
    assert "now with" in student_mails[0].subject

    # Idempotent: a second sign-in creates nothing new.
    assert svc.convert_invites_for_faculty(faculty) == []
    assert WalletJoinRequest.objects.filter(faculty=faculty).count() == 2

    # The requests appear in the faculty's pending actions and join-request list, still unapproved.
    from iic_booking.equipment.pending_actions import collect_pending_actions

    items = {i["key"]: i for i in collect_pending_actions(faculty)}
    assert items["wallet_join_requests"]["count"] == 2
    listed = _client(faculty).get("/api/wallet/join-requests/").data["requests"]
    assert {r["status"] for r in listed} == {"PENDING"}


def test_conversion_reuses_an_existing_request_instead_of_duplicating(student):
    _invite(_client(student))
    from iic_booking.users.models import Wallet

    faculty = _faculty()
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    existing = WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.PENDING
    )
    svc.convert_invites_for_faculty(faculty)
    assert WalletJoinRequest.objects.count() == 1
    assert SupervisorInvite.objects.get().join_request == existing


def test_non_faculty_sign_in_with_matching_email_does_not_convert(student):
    _invite(_client(student))
    staff = UserFactory(email=PROF, user_type=UserType.OPERATOR, admin_approved=True)
    assert svc.convert_invites_for_faculty(staff) == []
    assert SupervisorInvite.objects.get().status == SupervisorInviteStatus.PENDING


def test_email_password_login_converts_invites(student):
    _invite(_client(student))
    faculty = _faculty(email_verified=True)
    faculty.set_password("Str0ng-pass-123!")
    faculty.email_login_enabled = True
    faculty.last_login = timezone.now() - timedelta(days=3)
    faculty.save()
    res = APIClient().post(
        "/api/auth/login/", {"email": PROF, "password": "Str0ng-pass-123!"}, format="json"
    )
    assert res.status_code == 200, res.data
    jr = WalletJoinRequest.objects.get(faculty=faculty, student=student)
    assert jr.status == WalletJoinRequestStatus.PENDING


def test_faculty_join_request_list_also_converts(student):
    _invite(_client(student))
    faculty = _faculty()
    rows = _client(faculty).get("/api/wallet/join-requests/").data["requests"]
    assert len(rows) == 1 and rows[0]["status"] == "PENDING"


def test_resolve_token_routes_matching_faculty_only(student):
    _invite(_client(student))
    body = mail.outbox[0].body
    from urllib.parse import parse_qs, unquote, urlparse

    link = next(part for part in body.split() if "supervisor_invite" in part)
    target = unquote(parse_qs(urlparse(link).query)["next"][0])
    token = parse_qs(urlparse(target).query)["supervisor_invite"][0].split("#")[0]

    faculty = _faculty()
    res = _client(faculty).get(f"{URL}resolve/", {"token": token})
    assert res.data["matched"] is True
    assert res.data["join_request_id"] == WalletJoinRequest.objects.get().id
    assert res.data["join_request_status"] == "PENDING"

    stranger = _faculty("stranger@iitr.ac.in")
    assert _client(stranger).get(f"{URL}resolve/", {"token": token}).data == {"matched": False}
    assert _client(student).get(f"{URL}resolve/", {"token": token}).data == {"matched": False}
    assert _client(faculty).get(f"{URL}resolve/", {"token": "wrong"}).data == {"matched": False}
    # The token never signs anyone in.
    assert APIClient().get(f"{URL}resolve/", {"token": token}).status_code in (401, 403)
