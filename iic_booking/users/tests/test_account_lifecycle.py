"""Programme / employment end date: derivation, suspension (read-only), extensions and automatic wallet links."""

from __future__ import annotations

import json
import re
import uuid
from datetime import timedelta
from io import StringIO

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.users import account_lifecycle as lc
from iic_booking.users import registration_approvals as svc
from iic_booking.users.identity.dates import add_calendar_months, add_calendar_years
from iic_booking.users.models import (
    AccountLifecycle,
    AccountLifecycleEvent,
    Department,
    DepartmentType,
    DegreeCode,
    EndDateSource,
    LifecycleCategory,
    RegistrationExtensionRequest,
    RegistrationExtensionStatus,
    Wallet,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.suspension_middleware import clear_token_cache
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

POSTDOC = "IITR Post Doctoral Fellows"
RA = "IITR Research Associates in Projects"
E = AccountLifecycleEvent.Action


@pytest.fixture(autouse=True)
def _frontend(settings):
    settings.FRONTEND_URL = "https://equip.example.test"
    settings.ACCOUNT_END_DATE_ENFORCEMENT = False
    clear_token_cache()


@pytest.fixture
def enforce(settings):
    settings.ACCOUNT_END_DATE_ENFORCEMENT = True
    return settings


@pytest.fixture
def dept():
    return Department.objects.create(name="Chemistry", code="CY", department_type=DepartmentType.INTERNAL)


def _faculty(dept, email, name):
    user = UserFactory(email=email, name=name, user_type=UserType.FACULTY, admin_approved=True, department=dept)
    Wallet.objects.get_or_create(user=user)
    return user


@pytest.fixture
def faculty(dept):
    return _faculty(dept, "prof.sharma@iitr.ac.in", "Rakesh Sharma")


@pytest.fixture
def other_faculty(dept):
    return _faculty(dept, "prof.gupta@iitr.ac.in", "Meena Gupta")


@pytest.fixture
def admin():
    return UserFactory(email="main.admin@iitr.ac.in", name="Main Admin", user_type=UserType.ADMIN, admin_approved=True)


def _student(*, degree="", joining=None, graduation=None, faculty=None, **kw):
    user = UserFactory(
        email=f"s.{uuid.uuid4().hex[:6]}@iitr.ac.in",
        name="Ravi Kumar",
        user_type=UserType.STUDENT,
        admin_approved=True,
        degree_name=degree,
        joining_date=joining,
        graduation_date=graduation,
        **kw,
    )
    if faculty is not None:
        WalletJoinRequest.objects.create(student=user, faculty=faculty, status=WalletJoinRequestStatus.APPROVED)
    return user


def _postdoc(faculty, *, end=None, approved=True, alias=POSTDOC, **kw):
    today = timezone.localdate()
    return UserFactory(
        email=f"pd.{uuid.uuid4().hex[:6]}@gmail.com",
        name="Asha Verma",
        user_type=UserType.STUDENT,
        user_type_alias=alias,
        supervisor=faculty,
        email_verified=True,
        admin_approved=approved,
        supervisor_approved=approved,
        program_start_date=today - timedelta(days=100),
        program_end_date=end,
        **kw,
    )


def _token_client(user) -> APIClient:
    client = APIClient()
    token, _ = Token.objects.get_or_create(user=user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def _ended_student(faculty, days_ago=3):
    today = timezone.localdate()
    return _student(degree="M.Sc. - Master of Science", graduation=today - timedelta(days=days_ago), faculty=faculty)


def _events(user) -> list[str]:
    return list(AccountLifecycleEvent.objects.filter(user=user).order_by("id").values_list("action", flat=True))


# --- degree mapping and end date ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name,code",
    [
        ("Ph.D. - Doctor of Philosophy", DegreeCode.PHD),
        ("PhD", DegreeCode.PHD),
        ("Doctor of Philosophy", DegreeCode.PHD),
        ("M.Sc. - Master of Science", DegreeCode.MSC),
        ("MSc", DegreeCode.MSC),
        ("M.Tech. - Master of Technology", DegreeCode.MTECH),
        ("MTech", DegreeCode.MTECH),
        ("Int. M.Tech - Integrated Master of Technology", DegreeCode.INT_MTECH),
        ("Integrated M.Tech.", DegreeCode.INT_MTECH),
        ("IMTech", DegreeCode.INT_MTECH),
        ("B.Tech. - Bachelor of Technology", DegreeCode.BTECH),
        ("BTech", DegreeCode.BTECH),
        ("Int. M.Sc.", DegreeCode.OTHER),
        ("B.Tech + M.Tech (Dual Degree)", DegreeCode.OTHER),
        ("MBA - Master of Business Administration", DegreeCode.OTHER),
        ("B.Arch.", DegreeCode.OTHER),
        ("", ""),
        (None, ""),
    ],
)
def test_degree_names_map_to_rules(name, code):
    assert lc.normalise_degree(name) == code


@pytest.mark.parametrize(
    "degree,years",
    [
        ("Ph.D. - Doctor of Philosophy", 5),
        ("M.Sc. - Master of Science", 2),
        ("M.Tech. - Master of Technology", 2),
        ("Int. M.Tech - Integrated Master of Technology", 5),
        ("B.Tech. - Bachelor of Technology", 4),
        ("MBA", 5),
        ("", 5),
    ],
)
def test_student_end_date_is_joining_plus_degree_duration(degree, years):
    joining = timezone.localdate().replace(month=7, day=20) - timedelta(days=365)
    end = lc.compute(_student(degree=degree, joining=joining))
    assert end.category == LifecycleCategory.IITR_STUDENT
    assert end.programme_end_date == add_calendar_years(joining, years)
    assert end.source == EndDateSource.DERIVED_FROM_JOINING


def test_graduation_date_wins_over_derivation_and_missing_data_is_reported():
    today = timezone.localdate()
    grad = today + timedelta(days=40)
    end = lc.compute(_student(degree="Ph.D.", joining=today - timedelta(days=100), graduation=grad))
    assert end.programme_end_date == grad and end.source == EndDateSource.GRADUATION_DATE

    missing = lc.compute(_student(degree="Ph.D."))
    assert missing.programme_end_date is None and missing.source == EndDateSource.MISSING


def test_categories_and_supervised_users_use_their_registration_date(faculty):
    end = timezone.localdate() + timedelta(days=90)
    assert lc.category_for(_postdoc(faculty, end=end)) == LifecycleCategory.IITR_POSTDOC
    ra_user = _postdoc(faculty, end=end, alias=RA)
    assert lc.category_for(ra_user) == LifecycleCategory.IITR_RESEARCH_ASSOCIATE
    computed = lc.compute(ra_user)
    assert computed.programme_end_date == end and computed.source == EndDateSource.USER_PROVIDED
    startup = UserFactory(email="s@startup.in", user_type=UserType.STARTUP_INCUBATED_IITR, admin_approved=True, program_end_date=end)
    assert lc.category_for(startup) == LifecycleCategory.IITR_STARTUP
    assert lc.category_for(faculty) == "" and lc.compute(faculty) is None
    external = UserFactory(email="x@company.com", user_type=UserType.RND, admin_approved=True)
    assert lc.category_for(external) == ""


def test_profile_payload_shows_degree_end_date_and_source(faculty):
    joining = timezone.localdate() - timedelta(days=200)
    user = _student(degree="M.Tech. - Master of Technology", joining=joining, faculty=faculty)
    data = _client(user).get("/api/account-lifecycle/me/").data
    assert data["in_scope"] is True
    assert data["degree_name"] == "M.Tech. - Master of Technology"
    assert data["programme_end_date"] == add_calendar_years(joining, 2).isoformat()
    assert data["end_date_source"] == EndDateSource.DERIVED_FROM_JOINING
    assert "2 years" in data["end_date_source_note"] and "joining date" in data["end_date_source_note"].lower()
    assert data["supervisor"]["id"] == faculty.pk
    assert data["state"] == "active" and data["suspended"] is False

    me = _client(user).get("/api/auth/user/").data
    assert me["account_lifecycle"]["programme_end_date"] == data["programme_end_date"]
    assert _client(faculty).get("/api/account-lifecycle/me/").data == {"in_scope": False}


def test_profile_change_recomputes_the_stored_date():
    user = _student(degree="M.Sc.", joining=timezone.localdate() - timedelta(days=30))
    assert lc.lifecycle_for(user).degree_code == DegreeCode.MSC
    user.degree_name = "Ph.D."
    user.save(update_fields=["degree_name"])
    row = AccountLifecycle.objects.get(user=user)
    assert row.degree_code == DegreeCode.PHD
    assert row.programme_end_date == add_calendar_years(user.joining_date, 5)


# --- suspension -------------------------------------------------------------------------------------


def test_nothing_is_suspended_while_enforcement_is_off(faculty):
    user = _ended_student(faculty)
    assert not lc.is_suspended(user)
    res = _token_client(user).post("/api/equipments/999999/book/", {}, format="json")
    assert res.status_code != 403 or res.json().get("code") != lc.SUSPENDED_CODE
    assert lc.run_daily()["enforcement"] is False
    assert len(mail.outbox) == 0


def test_suspended_user_can_read_but_not_write(enforce, faculty):
    user = _ended_student(faculty)
    client = _token_client(user)
    assert lc.is_suspended(user)

    me = client.get("/api/auth/user/")
    assert me.status_code == 200 and me.data["account_lifecycle"]["state"] == "suspended"
    assert client.get("/api/account-lifecycle/me/").status_code == 200

    for path in ("/api/equipments/999999/book/", "/api/wallet/recharge-request/", "/api/bookings/1/shares/"):
        res = client.post(path, {}, format="json")
        assert res.status_code == 403, path
        body = res.json()
        assert body["code"] == lc.SUSPENDED_CODE and "Request extension" in body["error"]

    assert client.post("/api/notifications/mark-all-read/", {}, format="json").status_code == 200
    assert client.patch(f"/api/users/{user.pk}/", {"user_guide_viewed": True}, format="json").status_code == 200
    upload = client.patch(f"/api/users/{user.pk}/", {"name": "x"}, format="multipart")
    assert upload.status_code == 403 and upload.json()["code"] == lc.SUSPENDED_CODE
    assert AccountLifecycleEvent.objects.filter(user=user, action=E.WRITE_REFUSED).count() == 1
    assert user.__class__.objects.get(pk=user.pk).is_active


def test_session_user_is_refused_too(enforce, faculty):
    user = _ended_student(faculty)
    client = APIClient()
    client.force_login(user)
    res = client.post("/api/equipments/999999/book/", {}, format="json")
    assert res.status_code == 403 and res.json()["code"] == lc.SUSPENDED_CODE


def test_staff_cannot_book_on_behalf_of_suspended_user(enforce, faculty):
    user = _ended_student(faculty)
    refusal = lc.suspension_refusal(user, on_behalf=True)
    assert refusal["code"] == lc.SUSPENDED_CODE and "This user's account is suspended" in refusal["error"]
    active = _student(degree="Ph.D.", joining=timezone.localdate() - timedelta(days=10))
    assert lc.suspension_refusal(active) is None


def test_supervised_user_past_end_signs_in_when_enforced(enforce, faculty):
    user = _postdoc(faculty, end=timezone.localdate() - timedelta(days=1), password="Corr3ct-horse!")
    res = APIClient().post("/api/auth/login/", {"email": user.email, "password": "Corr3ct-horse!"}, format="json")
    assert res.status_code == 200, res.data
    assert lc.is_suspended(user)


def test_legacy_login_block_stays_while_enforcement_is_off(faculty):
    user = _postdoc(faculty, end=timezone.localdate() - timedelta(days=1), password="Corr3ct-horse!")
    res = APIClient().post("/api/auth/login/", {"email": user.email, "password": "Corr3ct-horse!"}, format="json")
    assert res.status_code == 403 and res.data["code"] == "programme_expired"


def test_daily_run_reminds_and_suspends_once(enforce, faculty):
    today = timezone.localdate()
    soon = _student(degree="M.Sc.", graduation=today + timedelta(days=7), faculty=faculty)
    ended = _ended_student(faculty, days_ago=1)
    long_ago = _ended_student(faculty, days_ago=400)

    first = lc.run_daily(today)
    assert first["reminded"] == 1 and first["suspended"] == 2
    reminder = next(m for m in mail.outbox if m.to == [soon.email])
    assert faculty.email in (reminder.cc or []) and "7 day(s)" in reminder.body
    suspended_mail = [m for m in mail.outbox if m.to == [ended.email]]
    assert len(suspended_mail) == 1 and "download your results" in suspended_mail[0].body
    assert not [m for m in mail.outbox if m.to == [long_ago.email]]
    assert E.SUSPENDED in _events(long_ago)

    count = len(mail.outbox)
    again = lc.run_daily(today)
    assert again["reminded"] == 0 and again["suspended"] == 0 and len(mail.outbox) == count
    assert lc.run_daily(today + timedelta(days=6))["reminded"] == 1
    ended.refresh_from_db()
    assert ended.is_active and not ended.force_inactive


def test_legacy_expiry_jobs_stand_down_when_enforced(enforce):
    assert svc.run_expiry() == {"ran": False, "reason": "account_lifecycle_enforcement"}


# --- extensions ------------------------------------------------------------------------------------


def test_student_extension_cycle_by_email_link(enforce, faculty):
    today = timezone.localdate()
    user = _ended_student(faculty)
    client = _token_client(user)

    res = client.post("/api/account-lifecycle/me/extension-request/", {"reason": "Thesis revision"}, format="json")
    assert res.status_code == 201, res.json()
    ext = RegistrationExtensionRequest.objects.get(user=user)
    assert ext.faculty == faculty and ext.max_until == add_calendar_months(today, 6)
    faculty_mail = mail.outbox[-1]
    assert faculty_mail.to == [faculty.email] and "M.Sc. - Master of Science" in faculty_mail.body
    raw = re.search(r"extension-decision\?token=([A-Za-z0-9_\-]+)", faculty_mail.body).group(1)

    view = APIClient().get("/api/account-lifecycle/extension-decision/", {"token": raw})
    assert view.status_code == 200 and view.data["item"]["id"] == ext.pk
    assert APIClient().post(
        "/api/account-lifecycle/extension-decision/", {"token": raw, "decision": "approve"}, format="json"
    ).data["code"] == "disclaimer_required"
    done = APIClient().post(
        "/api/account-lifecycle/extension-decision/",
        {"token": raw, "decision": "approve", "disclaimer_accepted": True, "disclaimer_version": svc.DISCLAIMER_VERSION},
        format="json",
    )
    assert done.status_code == 200 and done.data["decision"] == "approved"
    assert APIClient().get("/api/account-lifecycle/extension-decision/", {"token": raw}).status_code == 410

    lc.forget_cached(user)
    assert not lc.is_suspended(user)
    row = AccountLifecycle.objects.get(user=user)
    assert row.programme_end_date == ext.max_until and row.end_date_source == EndDateSource.EXTENDED
    assert row.suspended_at is None
    assert E.EXTENDED in _events(user) and E.RESUMED in _events(user)
    clear_token_cache()
    assert client.post("/api/equipments/999999/book/", {}, format="json").status_code != 403

    # Too early for another one; once the extension runs out the same cycle repeats.
    assert client.post("/api/account-lifecycle/me/extension-request/", {}, format="json").json()["reason"] == "too_early"
    RegistrationExtensionRequest.objects.filter(pk=ext.pk).update(approved_until=today - timedelta(days=1))
    lc.refresh(user)
    lc.forget_cached(user)
    assert lc.is_suspended(user)
    again = svc.request_extension(user)
    assert again.pk != ext.pk and again.max_until == add_calendar_months(today, 6)


def test_student_cannot_request_far_from_end_or_with_enforcement_off(faculty, settings):
    user = _student(degree="Ph.D.", joining=timezone.localdate() - timedelta(days=30), faculty=faculty)
    res = _client(user).post("/api/account-lifecycle/me/extension-request/", {}, format="json")
    assert res.status_code == 400 and res.data["reason"] == "enforcement_off"
    settings.ACCOUNT_END_DATE_ENFORCEMENT = True
    res = _client(user).post("/api/account-lifecycle/me/extension-request/", {}, format="json")
    assert res.status_code == 400 and res.data["reason"] == "too_early"


def test_extension_link_from_other_signed_in_faculty_is_refused(enforce, faculty, other_faculty):
    user = _ended_student(faculty)
    svc.request_extension(user)
    raw = re.search(r"extension-decision\?token=([A-Za-z0-9_\-]+)", mail.outbox[-1].body).group(1)
    res = _client(other_faculty).get("/api/account-lifecycle/extension-decision/", {"token": raw})
    assert res.status_code == 403 and res.data["code"] == "wrong_faculty"


def test_main_admin_extends_directly(enforce, admin, faculty):
    user = _ended_student(faculty)
    target = add_calendar_months(timezone.localdate(), 3)
    assert _client(faculty).get("/api/account-lifecycle/admin/report/").status_code == 403
    res = _client(admin).post(
        f"/api/account-lifecycle/admin/users/{user.pk}/extend/", {"until": target.isoformat(), "reason": "Viva date"}, format="json"
    )
    assert res.status_code == 200, res.data
    assert res.data["status"]["programme_end_date"] == target.isoformat() and res.data["status"]["suspended"] is False
    ext = RegistrationExtensionRequest.objects.get(user=user)
    assert ext.status == RegistrationExtensionStatus.APPROVED and ext.decided_role == "main_admin"


def test_supervisor_overview_lists_pending_and_nearing(enforce, faculty):
    today = timezone.localdate()
    nearing = _student(degree="M.Sc.", graduation=today + timedelta(days=20), faculty=faculty)
    _student(degree="M.Sc.", graduation=today + timedelta(days=400), faculty=faculty)
    ended = _ended_student(faculty)
    svc.request_extension(ended)
    data = _client(faculty).get("/api/account-lifecycle/supervisor/").data
    assert [p["user"]["id"] for p in data["pending_extensions"]] == [ended.pk]
    ids = {u["user"]["id"]: u for u in data["users"]}
    assert set(ids) == {nearing.pk, ended.pk}
    assert ids[ended.pk]["suspended"] is True and ids[nearing.pk]["days_left"] == 20


def test_missing_end_date_is_given_once_by_supervised_user(faculty):
    user = _postdoc(faculty, end=None)
    status = lc.status_payload(user)
    assert status["state"] == "end_date_missing" and status["end_date_required"] is True
    client = _client(user)
    too_far = add_calendar_years(timezone.localdate(), 6)
    assert client.post("/api/account-lifecycle/me/end-date/", {"end_date": too_far.isoformat()}, format="json").status_code == 400
    good = timezone.localdate() + timedelta(days=300)
    res = client.post("/api/account-lifecycle/me/end-date/", {"end_date": good.isoformat()}, format="json")
    assert res.status_code == 200 and res.data["status"]["programme_end_date"] == good.isoformat()
    again = client.post("/api/account-lifecycle/me/end-date/", {"end_date": good.isoformat()}, format="json")
    assert again.status_code == 400 and again.data["code"] == "already_set"


# --- automatic wallet link -------------------------------------------------------------------------


def _approve_registration(user, faculty):
    approval = svc.on_registration_verified(user)
    res = _client(faculty).post(
        f"/api/registration-approvals/{approval.pk}/decide/",
        {"decision": "approve", "disclaimer_accepted": True, "disclaimer_version": svc.DISCLAIMER_VERSION},
        format="json",
    )
    assert res.status_code == 200, res.data


def test_faculty_approval_links_postdoc_to_wallet(faculty):
    user = _postdoc(faculty, end=timezone.localdate() + timedelta(days=300), approved=False)
    _approve_registration(user, faculty)
    link = WalletJoinRequest.objects.get(student=user)
    assert link.faculty == faculty and link.status == WalletJoinRequestStatus.APPROVED
    user.refresh_from_db()
    assert user.get_accessible_wallet() == faculty.wallet
    linked_mail = [m for m in mail.outbox if "wallet" in m.subject.lower() and m.to == [user.email]]
    assert linked_mail and faculty.email in (linked_mail[-1].cc or [])
    assert E.WALLET_AUTO_LINKED in _events(user)


def test_auto_link_leaves_existing_links_and_startups_alone(faculty, other_faculty):
    end = timezone.localdate() + timedelta(days=300)
    elsewhere = _postdoc(faculty, end=end, alias=RA)
    WalletJoinRequest.objects.create(student=elsewhere, faculty=other_faculty, status=WalletJoinRequestStatus.APPROVED)
    assert lc.auto_link_wallet(elsewhere, faculty)[1] == "linked_elsewhere"

    pending = _postdoc(faculty, end=end)
    WalletJoinRequest.objects.create(student=pending, faculty=faculty, status=WalletJoinRequestStatus.PENDING)
    assert lc.auto_link_wallet(pending, faculty, notify=False)[1] == "approved_pending"
    assert WalletJoinRequest.objects.filter(student=pending, status=WalletJoinRequestStatus.APPROVED).count() == 1
    assert lc.auto_link_wallet(pending, faculty, notify=False)[1] == "already_linked"

    startup = UserFactory(email="s@startup.in", user_type=UserType.STARTUP_INCUBATED_IITR, admin_approved=True, supervisor=faculty)
    assert lc.auto_link_wallet(startup, faculty)[1] == "own_wallet"
    assert not WalletJoinRequest.objects.filter(student=startup).exists()


def test_backfill_dry_run_then_apply(faculty):
    end = timezone.localdate() + timedelta(days=300)
    todo = _postdoc(faculty, end=end)
    _postdoc(faculty, end=end, approved=False)
    test_account = _postdoc(faculty, end=end, is_test_account=True)

    dry = lc.backfill_links(apply=False)
    bucket = dry["by_category"][LifecycleCategory.IITR_POSTDOC]
    assert bucket["would_create"] == 1 and bucket["test_account_skipped"] == 1
    assert not WalletJoinRequest.objects.exists()

    done = lc.backfill_links(apply=True)
    assert done["linked_count"] == 1 and done["linked"][0]["user_id"] == todo.pk
    assert not WalletJoinRequest.objects.filter(student=test_account).exists()
    assert lc.backfill_links(apply=True)["linked_count"] == 0


# --- report and command ----------------------------------------------------------------------------


def test_report_is_read_only_counts(faculty):
    today = timezone.localdate()
    _ended_student(faculty)
    _student(degree="B.Tech. - Bachelor of Technology", joining=today - timedelta(days=100))
    _student(degree="Ph.D.")
    _postdoc(faculty, end=None)
    data = lc.report(today)
    students = data["by_category"][LifecycleCategory.IITR_STUDENT]
    assert students["active_users"] == 3 and students["would_suspend_now"] == 1 and students["missing_end_date"] == 1
    assert data["by_category"][LifecycleCategory.IITR_POSTDOC]["missing_end_date"] == 1
    assert data["iitr_students_by_degree"][DegreeCode.BTECH]["derived_from_joining"] == 1
    assert data["enforcement_enabled"] is False
    assert not AccountLifecycle.objects.filter(user__email__startswith="s.").exclude(user__degree_name="").filter(
        suspended_at__isnull=False
    ).exists()
    assert len(mail.outbox) == 0

    out = StringIO()
    call_command("account_lifecycle", "report", stdout=out)
    parsed = json.loads(out.getvalue())
    assert parsed["would_suspend_now_total"] == 1 and "@" not in out.getvalue()
