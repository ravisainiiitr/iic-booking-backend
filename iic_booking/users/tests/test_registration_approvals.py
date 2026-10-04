"""Registration requests: faculty approval, admin actions, audit log and programme expiry / extensions."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, DailySlot, Equipment, SlotMaster
from iic_booking.users import registration_approvals as svc
from iic_booking.users.identity.dates import add_calendar_months
from iic_booking.users.models import (
    Department,
    DepartmentType,
    RegistrationApproval,
    RegistrationApprovalEvent,
    RegistrationApprovalPolicy,
    RegistrationApprovalStatus,
    RegistrationApprovalToken,
    RegistrationExtensionRequest,
    RegistrationExtensionStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

ADMIN_URL = "/api/admin/registration-requests/"
FACULTY_URL = "/api/registration-approvals/"
POSTDOC = "IITR Post Doctoral Fellows"
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
def other_faculty(dept):
    return UserFactory(
        email="prof.gupta@iitr.ac.in", name="Meena Gupta", user_type=UserType.FACULTY, admin_approved=True, department=dept
    )


@pytest.fixture
def admin():
    return UserFactory(email="main.admin@iitr.ac.in", name="Main Admin", user_type=UserType.ADMIN, admin_approved=True)


def _postdoc(faculty, *, email=None, end=None, approved=False, **kw):
    today = timezone.localdate()
    return UserFactory(
        email=email or f"pd.{uuid.uuid4().hex[:6]}@gmail.com",
        name="Asha Verma",
        user_type=UserType.STUDENT,
        user_type_alias=POSTDOC,
        supervisor=faculty,
        email_verified=True,
        admin_approved=approved,
        supervisor_approved=approved,
        program_start_date=today - timedelta(days=100),
        program_end_date=end or (today + timedelta(days=200)),
        **kw,
    )


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user)
    return client


def _actions(user) -> list[str]:
    return list(RegistrationApprovalEvent.objects.filter(user=user).order_by("id").values_list("action", flat=True))


def _issue(approval=None, extension=None, faculty=None, **kw):
    raw, row = svc._issue_token(faculty=faculty, approval=approval, extension=extension)
    if kw:
        RegistrationApprovalToken.objects.filter(pk=row.pk).update(**kw)
    return raw


def _approve_payload(**extra):
    return {"decision": "approve", "disclaimer_accepted": True, "disclaimer_version": svc.DISCLAIMER_VERSION, **extra}


# --- new registration goes to the faculty -----------------------------------------------------


def test_verified_postdoc_registration_is_forwarded_to_faculty(faculty):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)

    assert approval.status == RegistrationApprovalStatus.PENDING_FACULTY
    assert approval.faculty == faculty
    assert _actions(user) == [A.SUBMITTED, A.FORWARDED, A.USER_NOTIFIED]
    assert RegistrationApprovalToken.objects.filter(approval=approval, used_at__isnull=True).count() == 1
    assert [m.to for m in mail.outbox] == [[faculty.email], [user.email]]
    body = mail.outbox[0].body
    assert "Asha Verma" in body and "working under my supervision" in body
    assert "registration-approvals" in body and "Pending approvals" in body
    assert "/registration-decision?token=" in body and "within 24 hours" in body


def test_external_registration_is_recorded_but_not_forwarded(faculty):
    user = UserFactory(email="ext@company.com", user_type=UserType.RND, email_verified=True, admin_approved=False)
    approval = svc.on_registration_verified(user)
    assert approval.status == RegistrationApprovalStatus.PENDING_ADMIN
    assert _actions(user) == [A.SUBMITTED]
    assert len(mail.outbox) == 0


def test_channel_i_accounts_are_out_of_scope(faculty, dept):
    student = UserFactory(email="s@iitr.ac.in", user_type=UserType.STUDENT, department=dept, admin_approved=True)
    assert svc.on_registration_verified(student) is None
    assert not svc.scoped_users().filter(pk=student.pk).exists()


# --- faculty decision ----------------------------------------------------------------------------


def test_faculty_must_tick_disclaimer_to_approve(faculty):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    client = _client(faculty)

    res = client.post(f"{FACULTY_URL}{approval.pk}/decide/", {"decision": "approve"}, format="json")
    assert res.status_code == 400 and res.data["code"] == "disclaimer_required"

    res = client.post(
        f"{FACULTY_URL}{approval.pk}/decide/", _approve_payload(disclaimer_version="old"), format="json"
    )
    assert res.status_code == 409 and res.data["code"] == "disclaimer_outdated"

    res = client.post(f"{FACULTY_URL}{approval.pk}/decide/", _approve_payload(), format="json")
    assert res.status_code == 200, res.data
    user.refresh_from_db()
    assert user.admin_approved and user.supervisor_approved and user.is_active
    assert svc.compute_status(user, RegistrationApproval.objects.get(pk=approval.pk)) == "approved"
    event = RegistrationApprovalEvent.objects.get(user=user, action=A.APPROVED)
    assert event.actor == faculty and event.actor_role == "faculty"
    assert event.details["disclaimer_version"] == svc.DISCLAIMER_VERSION
    assert "working under my supervision" in event.details["disclaimer_text"]
    approved_mail = mail.outbox[-1]
    assert approved_mail.to == [user.email] and faculty.email in (approved_mail.cc or [])


def test_disapprove_needs_reason_and_emails_user(faculty):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    client = _client(faculty)
    res = client.post(f"{FACULTY_URL}{approval.pk}/decide/", {"decision": "disapprove"}, format="json")
    assert res.status_code == 400 and res.data["code"] == "reason_required"
    res = client.post(
        f"{FACULTY_URL}{approval.pk}/decide/", {"decision": "disapprove", "reason": "Not in my group"}, format="json"
    )
    assert res.status_code == 200
    assert res.data["item"]["account_removed"] is True
    assert not type(user).objects.filter(email=user.email).exists()
    assert "Not in my group" in mail.outbox[-1].body
    assert mail.outbox[-1].to == [user.email] and faculty.email in (mail.outbox[-1].cc or [])


def test_wrong_faculty_gets_403_and_is_logged(faculty, other_faculty):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    raw = _issue(approval=approval, faculty=faculty)
    client = _client(other_faculty)

    assert client.get(f"{FACULTY_URL}{approval.pk}/").status_code == 403
    res = client.post(f"{FACULTY_URL}{approval.pk}/decide/", _approve_payload(), format="json")
    assert res.status_code == 403

    res = client.get(f"{FACULTY_URL}review/", {"token": raw})
    assert res.status_code == 403 and res.data["code"] == "wrong_faculty"
    res = client.post(f"{FACULTY_URL}{approval.pk}/decide/", _approve_payload(token=raw), format="json")
    assert res.status_code == 403 and res.data["code"] == "wrong_faculty"
    assert RegistrationApprovalEvent.objects.filter(action=A.TOKEN_REFUSED, actor=other_faculty).count() == 2
    user.refresh_from_db()
    assert not user.admin_approved


def test_token_is_single_use_and_expires(faculty):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    raw = _issue(approval=approval, faculty=faculty)
    client = _client(faculty)

    res = client.get(f"{FACULTY_URL}review/", {"token": raw})
    assert res.status_code == 200 and res.data["kind"] == "registration"
    assert RegistrationApprovalEvent.objects.filter(user=user, action=A.VIEWED, channel="email_link").exists()

    res = client.post(f"{FACULTY_URL}{approval.pk}/decide/", _approve_payload(token=raw), format="json")
    assert res.status_code == 200
    assert RegistrationApprovalEvent.objects.get(user=user, action=A.APPROVED).channel == "email_link"

    res = client.post(f"{FACULTY_URL}{approval.pk}/decide/", _approve_payload(token=raw), format="json")
    assert res.status_code == 410 and res.data["code"] == "already_decided"

    user2 = _postdoc(faculty)
    approval2 = svc.on_registration_verified(user2)
    expired = _issue(approval=approval2, faculty=faculty, expires_at=timezone.now() - timedelta(minutes=1))
    res = client.get(f"{FACULTY_URL}review/", {"token": expired})
    assert res.status_code == 410 and res.data["code"] == "token_expired"
    assert client.get(f"{FACULTY_URL}review/", {"token": "nonsense"}).status_code == 404


def test_reforward_retires_earlier_links(faculty, admin):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    first = _issue(approval=approval, faculty=faculty)
    svc.remind(user, actor=admin)
    with pytest.raises(svc.ApprovalError) as err:
        svc.resolve_token(first, faculty)
    assert err.value.code == "token_used"
    assert A.REMINDER_SENT in _actions(user)


# --- main administrator --------------------------------------------------------------------------


def test_admin_endpoints_are_main_admin_only(faculty, dept):
    dept_admin = UserFactory(email="da@iitr.ac.in", user_type=UserType.DEPT_ADMIN, admin_approved=True, department=dept)
    for who in (faculty, dept_admin):
        client = _client(who)
        assert client.get(ADMIN_URL).status_code == 403
        assert client.get(f"{ADMIN_URL}log/").status_code == 403
        assert client.post(f"{ADMIN_URL}bulk-forward/", {"confirm_count": 0}, format="json").status_code == 403
        assert client.post(f"{ADMIN_URL}automation/", {"enabled": True, "confirm": "ENABLE"}, format="json").status_code == 403


def test_admin_list_detail_and_filters(admin, faculty):
    pending = _postdoc(faculty)
    svc.on_registration_verified(pending)
    external = UserFactory(email="ext2@company.com", user_type=UserType.RND, email_verified=True)
    UserFactory(email="qa@iic-test.example", user_type=UserType.RND, email_verified=True, is_test_account=True)
    client = _client(admin)

    res = client.get(ADMIN_URL)
    assert res.status_code == 200
    ids = {r["user_id"] for r in res.data["results"]}
    assert {pending.pk, external.pk} <= ids
    assert all(r["email"] != "qa@iic-test.example" for r in res.data["results"])
    assert res.data["summary"]["by_status"]["pending_faculty"] == 1

    res = client.get(ADMIN_URL, {"status": "pending_faculty"})
    assert [r["user_id"] for r in res.data["results"]] == [pending.pk]
    res = client.get(ADMIN_URL, {"claims_iitr": "no"})
    assert pending.pk not in {r["user_id"] for r in res.data["results"]}

    res = client.get(f"{ADMIN_URL}{pending.pk}/")
    assert res.status_code == 200
    assert [e["action"] for e in res.data["timeline"]][:2] in ([A.FORWARDED, A.SUBMITTED], [A.SUBMITTED, A.FORWARDED])


def test_admin_reject_requires_reason_and_admin_approve_overrides(admin, faculty):
    user = _postdoc(faculty)
    svc.on_registration_verified(user)
    client = _client(admin)
    res = client.post(f"{ADMIN_URL}{user.pk}/reject/", {}, format="json")
    assert res.status_code == 400 and res.data["code"] == "reason_required"

    res = client.post(f"{ADMIN_URL}{user.pk}/approve/", {}, format="json")
    assert res.status_code == 200
    user.refresh_from_db()
    assert user.admin_approved and user.supervisor_approved and user.is_active
    event = RegistrationApprovalEvent.objects.get(user=user, action=A.ADMIN_OVERRIDE)
    assert event.details["previous_status"] == "pending_faculty" and event.actor_role == "main_admin"
    assert not RegistrationApprovalToken.objects.filter(approval__user=user, used_at__isnull=True).exists()


def test_admin_bulk_forward_existing_requests(admin, faculty, dept):
    old = [_postdoc(faculty) for _ in range(2)]
    missing = _postdoc(None)
    UserFactory(email="startup@x.com", user_type=UserType.STARTUP_INCUBATED_IITR, email_verified=True)
    client = _client(admin)

    preview = client.get(f"{ADMIN_URL}bulk-forward/")
    assert preview.status_code == 200 and preview.data["count"] == 2

    assert client.post(f"{ADMIN_URL}bulk-forward/", {}, format="json").status_code == 400
    res = client.post(f"{ADMIN_URL}bulk-forward/", {"confirm_count": 5}, format="json")
    assert res.status_code == 409 and res.data["code"] == "count_changed" and res.data["count"] == 2
    assert len(mail.outbox) == 0

    res = client.post(f"{ADMIN_URL}bulk-forward/", {"confirm_count": 2}, format="json")
    assert res.status_code == 200 and res.data["forwarded"] == 2
    for user in old:
        assert RegistrationApproval.objects.get(user=user).status == RegistrationApprovalStatus.PENDING_FACULTY
        ev = RegistrationApprovalEvent.objects.get(user=user, action=A.FORWARDED)
        assert ev.actor == admin and ev.actor_role == "main_admin"
    # One request to the faculty member and one "sent to your supervisor" notice per user.
    assert len(mail.outbox) == 4
    assert not RegistrationApproval.objects.filter(user=missing).exists()
    assert client.get(f"{ADMIN_URL}bulk-forward/").data["count"] == 0


def test_change_faculty_needs_reason_and_retires_link(admin, faculty, other_faculty):
    user = _postdoc(faculty)
    approval = svc.on_registration_verified(user)
    client = _client(admin)
    res = client.post(f"{ADMIN_URL}{user.pk}/change-faculty/", {"faculty_id": other_faculty.pk}, format="json")
    assert res.status_code == 400 and res.data["code"] == "reason_required"
    res = client.post(
        f"{ADMIN_URL}{user.pk}/change-faculty/",
        {"faculty_id": other_faculty.pk, "reason": "Moved labs", "forward": True},
        format="json",
    )
    assert res.status_code == 200
    approval.refresh_from_db()
    user.refresh_from_db()
    assert approval.faculty == other_faculty and user.supervisor == other_faculty
    assert approval.status == RegistrationApprovalStatus.PENDING_FACULTY
    assert RegistrationApprovalToken.objects.filter(approval=approval, used_at__isnull=True).get().faculty == other_faculty
    assert A.FACULTY_CHANGED in _actions(user)
    assert _client(faculty).post(f"{FACULTY_URL}{approval.pk}/decide/", _approve_payload(), format="json").status_code == 403


def test_log_lists_events_and_exports_csv(admin, faculty):
    user = _postdoc(faculty)
    svc.on_registration_verified(user)
    client = _client(admin)
    res = client.get(f"{ADMIN_URL}log/", {"action": A.FORWARDED})
    assert res.status_code == 200 and res.data["count"] == 1
    assert res.data["results"][0]["actor_role"] == "system"
    csv_res = client.get(f"{ADMIN_URL}log/", {"export": "csv"})
    assert csv_res.status_code == 200
    text = csv_res.content.decode()
    assert text.startswith("Time,Action,User") and user.email in text


# --- programme expiry and extensions ---------------------------------------------------------------


def _booking(owner):
    equipment = Equipment.objects.create(
        name="XRD", code=f"RA{uuid.uuid4().hex[:4].upper()}", slot_duration_minutes=60, user_rating_enabled=False
    )
    profile = ChargeProfile.objects.create(equipment=equipment, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10"))
    booking = Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.BOOKED,
        total_charge=Decimal("10"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}2026{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )
    start = timezone.now() + timedelta(days=3)
    end = start + timedelta(hours=1)
    master = SlotMaster.objects.create(
        equipment=equipment, slot_number=1, open_time=start.time().replace(microsecond=0),
        close_time=end.time().replace(microsecond=0), is_active=True,
    )
    DailySlot.objects.create(
        slot_master=master, date=start.date(), start_datetime=start, end_datetime=end, status="BOOKED", booking=booking
    )
    return booking


def _enable():
    RegistrationApprovalPolicy.objects.update_or_create(pk=1, defaults={"expiry_automation_enabled": True})


def test_expiry_job_is_a_no_op_until_enabled(faculty):
    today = timezone.localdate()
    expired = _postdoc(faculty, approved=True, end=today - timedelta(days=2))
    _postdoc(faculty, approved=True, end=today + timedelta(days=5))

    assert svc.run_expiry() == {"ran": False, "reason": "disabled"}
    expired.refresh_from_db()
    assert not expired.force_inactive and len(mail.outbox) == 0

    report = svc.dry_run()
    assert report["counts"] == {"would_warn": 1, "would_disable": 1}
    assert report["would_warn"][0]["days"] == 7
    expired.refresh_from_db()
    assert not expired.force_inactive and len(mail.outbox) == 0 and not RegistrationApprovalEvent.objects.exists()


def test_expiry_warns_once_per_threshold(faculty):
    _enable()
    today = timezone.localdate()
    user = _postdoc(faculty, approved=True, end=today + timedelta(days=6))
    assert svc.run_expiry(today)["warned"] == 1
    assert svc.run_expiry(today)["warned"] == 0
    warning = mail.outbox[-1]
    assert warning.to == [user.email] and faculty.email in (warning.cc or [])
    assert "six months" in warning.body and "/programme-extension?token=" in warning.body
    assert svc.run_expiry(today + timedelta(days=5))["warned"] == 1
    assert _actions(user).count(A.EXPIRY_WARNING) == 2


def test_expiry_disables_but_keeps_future_bookings(faculty, admin):
    _enable()
    today = timezone.localdate()
    user = _postdoc(faculty, approved=True, end=today - timedelta(days=1))
    booking = _booking(user)

    assert svc.run_expiry(today)["disabled"] == 1
    user.refresh_from_db()
    booking.refresh_from_db()
    assert user.force_inactive and not user.is_active
    assert booking.status == BookingStatus.BOOKED
    event = RegistrationApprovalEvent.objects.get(user=user, action=A.DISABLED)
    assert event.details["future_bookings_kept"]
    assert "six months" in mail.outbox[-1].body

    detail = _client(admin).get(f"{ADMIN_URL}{user.pk}/").data
    assert [b["booking_id"] for b in detail["future_bookings"]] == [booking.booking_id]
    automation = _client(admin).get(f"{ADMIN_URL}automation/").data
    assert automation["disabled_with_future_bookings"][0]["user_id"] == user.pk


def test_extension_cap_and_re_enable(faculty):
    _enable()
    today = timezone.localdate()
    end = today - timedelta(days=3)
    user = _postdoc(faculty, approved=True, end=end)
    svc.run_expiry(today)

    token = svc.make_user_extension_token(user)
    res = APIClient().post(f"{FACULTY_URL}extension-request/", {"token": token, "reason": "Project extended", "channel": "login"}, format="json")
    assert res.status_code == 201, res.data
    assert "six months" in res.data["message"]
    ext = RegistrationExtensionRequest.objects.get(user=user)
    assert ext.max_until == add_calendar_months(today, 6)
    assert mail.outbox[-1].to == [faculty.email] and "six months" in mail.outbox[-1].body

    client = _client(faculty)
    url = f"{FACULTY_URL}extensions/{ext.pk}/decide/"
    too_long = (ext.max_until + timedelta(days=1)).isoformat()
    res = client.post(url, _approve_payload(until=too_long), format="json")
    assert res.status_code == 400 and res.data["code"] == "extension_too_long"
    res = client.post(url, {"decision": "approve", "until": ext.max_until.isoformat()}, format="json")
    assert res.status_code == 400 and res.data["code"] == "disclaimer_required"

    shorter = today + timedelta(days=60)
    res = client.post(url, _approve_payload(until=shorter.isoformat()), format="json")
    assert res.status_code == 200, res.data
    user.refresh_from_db()
    assert user.program_end_date == shorter and not user.force_inactive and user.is_active
    ext.refresh_from_db()
    assert ext.status == RegistrationExtensionStatus.APPROVED and "six months" in ext.disclaimer_text
    assert _actions(user)[-2:] == [A.EXTENSION_GRANTED, A.RE_ENABLED]
    granted = mail.outbox[-1]
    assert granted.to == [user.email] and faculty.email in (granted.cc or []) and "six months" in granted.body

    # A further extension is allowed and counts six months from the current validity.
    again = svc.request_extension(user)
    assert again.max_until == add_calendar_months(shorter, 6)


def test_extension_from_other_faculty_is_refused(faculty, other_faculty):
    user = _postdoc(faculty, approved=True, end=timezone.localdate() + timedelta(days=10))
    ext = svc.request_extension(user)
    res = _client(other_faculty).post(f"{FACULTY_URL}extensions/{ext.pk}/decide/", _approve_payload(), format="json")
    assert res.status_code == 403


def test_admin_extension_uses_same_cap(admin, faculty):
    today = timezone.localdate()
    user = _postdoc(faculty, approved=True, end=today + timedelta(days=10))
    client = _client(admin)
    too_long = add_calendar_months(today + timedelta(days=10), 6) + timedelta(days=1)
    res = client.post(f"{ADMIN_URL}{user.pk}/extend/", {"until": too_long.isoformat(), "reason": "Letter"}, format="json")
    assert res.status_code == 400 and res.data["code"] == "extension_too_long"
    ok = add_calendar_months(today + timedelta(days=10), 6)
    res = client.post(f"{ADMIN_URL}{user.pk}/extend/", {"until": ok.isoformat(), "reason": "Letter"}, format="json")
    assert res.status_code == 200
    user.refresh_from_db()
    assert user.program_end_date == ok


def test_automation_switch_requires_confirmation_and_is_logged(admin):
    client = _client(admin)
    assert client.post(f"{ADMIN_URL}automation/", {"enabled": True}, format="json").status_code == 400
    res = client.post(f"{ADMIN_URL}automation/", {"enabled": True, "confirm": "ENABLE"}, format="json")
    assert res.status_code == 200 and res.data["enabled"] is True
    assert RegistrationApprovalEvent.objects.filter(action=A.AUTOMATION_CHANGED, actor=admin).count() == 1


def test_login_block_offers_extension_only_with_correct_password(faculty):
    user = _postdoc(faculty, approved=True, end=timezone.localdate() - timedelta(days=1), password="Corr3ct-horse!")
    res = APIClient().post("/api/auth/login/", {"email": user.email, "password": "wrong"}, format="json")
    assert res.status_code == 403 and res.data["code"] == "programme_expired"
    assert "extension_token" not in res.data
    res = APIClient().post("/api/auth/login/", {"email": user.email, "password": "Corr3ct-horse!"}, format="json")
    assert res.status_code == 403 and res.data["extension_token"]
    assert res.data["extension_max_months"] == 6
    assert svc.read_user_extension_token(res.data["extension_token"]) == user


def test_login_names_faculty_while_pending(faculty):
    user = _postdoc(faculty, password="Corr3ct-horse!")
    svc.on_registration_verified(user)
    res = APIClient().post("/api/auth/login/", {"email": user.email, "password": "Corr3ct-horse!"}, format="json")
    assert res.status_code == 403 and res.data["pending_faculty"] is True
    assert "Rakesh Sharma" in res.data["message"]


def test_management_command_dry_run_and_switch(faculty):
    import json
    from io import StringIO

    from django.core.management import call_command

    _postdoc(faculty, approved=True, end=timezone.localdate() - timedelta(days=1))
    out = StringIO()
    call_command("registration_approvals", "dry-run", stdout=out)
    data = json.loads(out.getvalue())
    assert data["automation_enabled"] is False and data["counts"]["would_disable"] == 1
    assert "@" not in out.getvalue()

    call_command("registration_approvals", "enable", stdout=StringIO())
    assert svc.automation_enabled()
    call_command("registration_approvals", "disable", stdout=StringIO())
    assert not svc.automation_enabled()
    assert RegistrationApprovalEvent.objects.filter(action=A.AUTOMATION_CHANGED, actor_role="system").count() == 2


def test_production_report_is_read_only(faculty):
    today = timezone.localdate()
    _postdoc(faculty)
    _postdoc(None)
    _postdoc(faculty, approved=True, end=today - timedelta(days=1))
    _postdoc(faculty, approved=True, end=today + timedelta(days=20))
    report = svc.production_report()
    assert report["pending_claiming_iitr"] == 2
    assert report["pending_claiming_iitr_missing_faculty"] == 1
    assert report["bulk_forward_candidates"] == 1
    assert report["approved_iitr_programme_expired"] == 1
    assert report["approved_iitr_programme_expiring_30_days"] == 1
    assert not RegistrationApprovalEvent.objects.exists() and len(mail.outbox) == 0
