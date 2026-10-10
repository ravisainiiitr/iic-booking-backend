"""Supervisor on the user card, the Users overview table and its export: one resolution, in precedence order."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.admin_insights.supervisors import resolve_supervisors
from iic_booking.users.models import Department, RegistrationApproval, SupervisorInvite
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    SubWalletTransaction,
    Wallet,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

POSTDOC = "IITR Post Doctoral Fellows"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def dept():
    return Department.objects.create(name="Chemistry Department", code="CY-SUP")


def _person(**fields):
    fields.setdefault("admin_approved", True)
    return UserFactory(**fields)


def _faculty(dept, name="Prof. Rao"):
    return _person(user_type=UserType.FACULTY, department=dept, name=name)


def _student(dept, **fields):
    return _person(user_type=UserType.STUDENT, department=dept, **fields)


def _wallet(owner, dept):
    wallet, _ = Wallet.objects.get_or_create(user=owner)
    sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=dept)
    return wallet, sub


def _join(student, owner, dept, status=WalletJoinRequestStatus.APPROVED):
    wallet, _ = _wallet(owner, dept)
    return WalletJoinRequest.objects.create(faculty=owner, student=student, wallet=wallet, status=status)


def _invite(student, dept, email, name="Dr. Invited"):
    return SupervisorInvite.objects.create(
        student=student,
        email=email,
        supervisor_name=name,
        department=dept,
        token_hash=hashlib.sha256(f"{student.pk}{email}".encode()).hexdigest(),
        expires_at=timezone.now() + timedelta(days=7),
    )


def _debit(student, owner, dept):
    _, sub = _wallet(owner, dept)
    return SubWalletTransaction.objects.create(
        sub_wallet=sub, transaction_type="debit", amount=Decimal("10.00"), description="Booking X", related_user=student
    )


def _resolve(user):
    return resolve_supervisors([user.pk]).get(user.pk)


def test_wallet_link_is_how_students_get_a_supervisor(dept):
    faculty, student = _faculty(dept), _student(dept)
    _join(student, faculty, dept)
    s = _resolve(student)
    assert (s["id"], s["source"], s["pending"]) == (faculty.pk, "wallet", False)
    assert s["department"] == "Chemistry Department" and s["email"] == faculty.email


def test_approved_profile_supervisor_comes_first(dept):
    profile, wallet_owner = _faculty(dept, "Prof. Profile"), _faculty(dept, "Prof. Wallet")
    postdoc = _person(user_type=UserType.OTHER, department=dept, user_type_alias=POSTDOC, supervisor=profile,
                      supervisor_approved=True)
    _join(postdoc, wallet_owner, dept)
    assert (_resolve(postdoc)["id"], _resolve(postdoc)["source"]) == (profile.pk, "profile")


def test_unapproved_profile_supervisor_yields_to_wallet_then_shows_pending(dept):
    profile, wallet_owner = _faculty(dept, "Prof. Profile"), _faculty(dept, "Prof. Wallet")
    postdoc = _person(user_type=UserType.OTHER, department=dept, user_type_alias=POSTDOC, supervisor=profile,
                      supervisor_approved=False)
    assert (_resolve(postdoc)["source"], _resolve(postdoc)["pending"]) == ("pending_profile", True)
    _join(postdoc, wallet_owner, dept)
    assert (_resolve(postdoc)["id"], _resolve(postdoc)["source"]) == (wallet_owner.pk, "wallet")


def test_registration_faculty(dept):
    faculty, student = _faculty(dept), _student(dept)
    approval = RegistrationApproval.objects.create(user=student, faculty=faculty, status="pending_faculty")
    assert (_resolve(student)["id"], _resolve(student)["source"], _resolve(student)["pending"]) == (
        faculty.pk, "pending_registration", True,
    )
    approval.status = "approved"
    approval.save(update_fields=["status"])
    assert (_resolve(student)["source"], _resolve(student)["pending"]) == ("registration", False)


def test_pending_wallet_request(dept):
    faculty, student = _faculty(dept), _student(dept)
    _join(student, faculty, dept, status=WalletJoinRequestStatus.PENDING)
    s = _resolve(student)
    assert (s["id"], s["source"], s["pending"]) == (faculty.pk, "pending_wallet", True)


def test_email_invite_to_a_supervisor_not_on_the_portal(dept):
    student = _student(dept)
    _invite(student, dept, "guide@iitr.ac.in", name="Dr. Guide")
    s = _resolve(student)
    assert s == {
        "id": None, "name": "Dr. Guide", "email": "guide@iitr.ac.in", "department": "Chemistry Department",
        "source": "pending_invite", "source_display": s["source_display"], "pending": True,
    }


def test_email_invite_matches_a_portal_user(dept):
    faculty, student = _faculty(dept), _student(dept)
    _invite(student, dept, faculty.email.upper())
    assert (_resolve(student)["id"], _resolve(student)["source"]) == (faculty.pk, "pending_invite")


def test_wallet_charged_for_the_latest_booking_is_the_last_resort(dept):
    old, recent, student = _faculty(dept, "Prof. Old"), _faculty(dept, "Prof. Recent"), _student(dept)
    first = _debit(student, old, dept)
    SubWalletTransaction.objects.filter(pk=first.pk).update(created_at=timezone.now() - timedelta(days=3))
    _debit(student, recent, dept)
    s = _resolve(student)
    assert (s["id"], s["source"], s["pending"]) == (recent.pk, "booking", False)
    _join(student, old, dept, status=WalletJoinRequestStatus.PENDING)
    assert _resolve(student)["source"] == "pending_wallet"


def test_no_link_and_faculty_have_none(dept):
    faculty, student = _faculty(dept), _student(dept)
    faculty.supervisor = _faculty(dept, "Prof. Other")
    faculty.save(update_fields=["supervisor"])
    assert resolve_supervisors([student.pk, faculty.pk]) == {}


def test_batch_query_count(dept, django_assert_max_num_queries):
    faculty = _faculty(dept)
    students = [_student(dept) for _ in range(6)]
    for s in students[:3]:
        _join(s, faculty, dept)
    _invite(students[3], dept, "x@iitr.ac.in")
    with django_assert_max_num_queries(9):
        found = resolve_supervisors([s.pk for s in students])
    assert len(found) == 4


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_card_table_and_export_show_the_wallet_supervisor(dept):
    admin = _person(user_type=UserType.ADMIN)
    faculty, student = _faculty(dept, "Prof. Rao"), _student(dept, name="Astitva")
    _join(student, faculty, dept)

    card = _client(admin).get(f"/api/admin/insights/users/{student.pk}/").json()
    assert card["profile"]["supervisor"]["id"] == faculty.pk
    assert card["profile"]["supervisor"]["source"] == "wallet"
    assert card["wallet"]["owner_id"] == faculty.pk

    rows = _client(admin).get("/api/admin/insights/users/", {"search": student.email}).json()["results"]
    row = next(r for r in rows if r["id"] == student.pk)
    assert row["supervisor"]["id"] == faculty.pk
    assert row["wallet_owner_id"] == faculty.pk

    resp = _client(admin).get("/api/exports/admin-users-overview/", {"export_format": "csv", "search": student.email})
    assert resp.status_code == 200
    body = b"".join(resp.streaming_content) if getattr(resp, "streaming", False) else resp.content
    text = body.decode("utf-8-sig")
    assert "Supervisor" in text and "Prof. Rao" in text


def test_department_admin_can_open_the_wallet_supervisor_of_their_student(dept):
    other_dept = Department.objects.create(name="Physics", code="PH-SUP")
    faculty = _faculty(other_dept)
    student = _student(dept)
    _join(student, faculty, other_dept)
    dept_admin = _person(user_type=UserType.DEPT_ADMIN, department=dept)
    assert _client(dept_admin).get(f"/api/admin/insights/users/{faculty.pk}/").status_code == 200
    stranger = _faculty(other_dept, "Prof. Stranger")
    assert _client(dept_admin).get(f"/api/admin/insights/users/{stranger.pk}/").status_code == 404
