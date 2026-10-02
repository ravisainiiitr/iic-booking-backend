"""IIT Roorkee faculty are shown as "Prof. <name>" in API names and emails; nobody else is, and the DB keeps the plain name."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from iic_booking.communication import styled_transactional_emails
from iic_booking.communication.email_branding import user_display_name
from iic_booking.communication.in_app import person_label
from iic_booking.users.display import (
    apply_faculty_name_prefix,
    clean_person_name,
    format_named_person,
    get_user_display_name,
)
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import WalletRechargeMode, WalletRechargeRequest
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Ravi Kumar", "Prof. Ravi Kumar"),
        ("  Ravi   Kumar ", "Prof. Ravi Kumar"),
        ("Prof. Ravi Kumar", "Prof. Ravi Kumar"),
        ("prof Ravi Kumar", "prof Ravi Kumar"),
        ("Professor Ravi Kumar", "Professor Ravi Kumar"),
        ("Dr. Ravi Kumar", "Dr. Ravi Kumar"),
        ("Dr Ravi Kumar", "Dr Ravi Kumar"),
        ("Prof. Dr. Ravi Kumar", "Dr. Ravi Kumar"),
        ("Prof. Prof. Ravi Kumar", "Prof. Ravi Kumar"),
        ("Drona Rao", "Prof. Drona Rao"),
        ("Professorial Rao", "Prof. Professorial Rao"),
        ("Prof.", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_faculty_prefix_rule(name, expected):
    assert apply_faculty_name_prefix(name, UserType.FACULTY) == expected


@pytest.mark.parametrize(
    "user_type", [UserType.STUDENT, UserType.EXTERNAL, UserType.MANAGER, UserType.OPERATOR, UserType.ADMIN, None, ""]
)
def test_non_faculty_names_are_unchanged(user_type):
    assert apply_faculty_name_prefix("Asha Verma", user_type) == "Asha Verma"
    assert apply_faculty_name_prefix("Dr. Asha Verma", user_type) == "Dr. Asha Verma"


def test_user_type_is_matched_case_insensitively():
    assert apply_faculty_name_prefix("Ravi Kumar", "Faculty") == "Prof. Ravi Kumar"


def test_clean_person_name_drops_only_a_redundant_prof():
    assert clean_person_name("Prof. Dr. X") == "Dr. X"
    assert clean_person_name("Prof Mrs. Y") == "Mrs. Y"
    assert clean_person_name("Dr.") == ""


def test_display_name_falls_back_to_email_without_prefix():
    faculty = SimpleNamespace(name="", email="ravi@iitr.ac.in", user_type=UserType.FACULTY)
    assert get_user_display_name(faculty) == "ravi@iitr.ac.in"
    assert get_user_display_name(faculty, fallback_to_email=False) == ""
    assert format_named_person("", UserType.FACULTY, "ravi@iitr.ac.in") == "ravi@iitr.ac.in"
    assert format_named_person("Ravi", UserType.FACULTY, "ravi@iitr.ac.in") == "Prof. Ravi"
    assert get_user_display_name(None) == ""


def test_email_and_notification_name_helpers_use_the_rule():
    faculty = SimpleNamespace(name="Ravi Kumar", email="ravi@iitr.ac.in", user_type=UserType.FACULTY)
    doctor = SimpleNamespace(name="Dr. Meena Rao", email="meena@iitr.ac.in", user_type=UserType.FACULTY)
    student = SimpleNamespace(name="Asha Verma", email="asha@iitr.ac.in", user_type=UserType.STUDENT)
    nameless = SimpleNamespace(name="", email="x@iitr.ac.in", user_type=UserType.FACULTY)

    assert user_display_name(faculty) == "Prof. Ravi Kumar"
    assert user_display_name(doctor) == "Dr. Meena Rao"
    assert user_display_name(student) == "Asha Verma"
    assert user_display_name(nameless) == "x@iitr.ac.in"
    assert user_display_name("Plain Text Name") == "Plain Text Name"
    assert person_label(faculty) == "Prof. Ravi Kumar"
    assert person_label(student) == "Asha Verma"
    assert person_label(None) == "a user"


@pytest.mark.django_db
def test_display_name_never_changes_the_stored_name():
    faculty = UserFactory(user_type=UserType.FACULTY, name="Ravi Kumar", email="ravi.store@iitr.ac.in")
    assert faculty.get_display_name() == "Prof. Ravi Kumar"
    faculty.refresh_from_db()
    assert faculty.name == "Ravi Kumar"


@pytest.fixture
def people(db):
    faculty = UserFactory(user_type=UserType.FACULTY, name="Ravi Kumar", email="ravi.kumar@iitr.ac.in")
    student = UserFactory(user_type=UserType.STUDENT, name="Asha Verma", email="asha.verma@iitr.ac.in")
    return SimpleNamespace(faculty=faculty, student=student)


@pytest.fixture
def outbox(monkeypatch):
    sent = []
    monkeypatch.setattr(
        styled_transactional_emails,
        "_send",
        lambda to, subject, text, html: sent.append(SimpleNamespace(to=to, subject=subject, text=text, html=html)),
    )
    return sent


def _join_request(people, **extra):
    return SimpleNamespace(
        id=101, faculty_id=people.faculty.id, student=people.student, faculty=people.faculty,
        message="Please add me", faculty_response="Welcome", **extra,
    )


def test_wallet_join_request_emails_name_the_faculty_with_prof(people, outbox):
    styled_transactional_emails.send_wallet_join_request_submitted_emails(_join_request(people))

    to_student = next(m for m in outbox if m.to == people.student.email)
    to_faculty = next(m for m in outbox if m.to == people.faculty.email)
    assert "Faculty: Prof. Ravi Kumar" in to_student.text
    assert "Prof. Ravi Kumar (ravi.kumar@iitr.ac.in)" in to_student.html
    assert "Student: Asha Verma (asha.verma@iitr.ac.in)" in to_faculty.text
    assert "Prof. Asha" not in to_faculty.text


def test_wallet_join_decision_email_names_the_faculty_with_prof(people, outbox):
    people.faculty.name = "Dr. Ravi Kumar"
    people.faculty.save(update_fields=["name"])

    styled_transactional_emails.send_wallet_join_request_decision_email(_join_request(people), "approved")

    (message,) = outbox
    assert "Faculty: Dr. Ravi Kumar (ravi.kumar@iitr.ac.in)" in message.text
    assert "Prof. Dr." not in message.html


@pytest.fixture
def recharge_setup(people):
    dept = Department.objects.create(name="Prof Prefix Dept", code="PPD", department_type=DepartmentType.INTERNAL)
    for user in (people.faculty, people.student):
        user.department = dept
        user.save(update_fields=["department"])
    wallet, _ = Wallet.objects.get_or_create(user=people.faculty)

    def make(user, mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT):
        return WalletRechargeRequest.objects.create(
            user=user, wallet=wallet, department=dept, amount=Decimal("500.00"), user_otp_verified=True,
            recharge_mode=mode, employee_number=user.emp_id or "E1", department_grant_code="IIC-000-002",
            project_grant_code="PRJ-1" if mode == WalletRechargeMode.PROJECT_GRANT else "",
        )

    return make


def test_recharge_email_supervisor_name_has_prof(people, recharge_setup):
    from iic_booking.users.wallet_recharge_workflow import requester_details_text, serialize_request_public

    req = recharge_setup(people.student)

    text = requester_details_text(req)
    assert "Supervisor Name: Prof. Ravi Kumar" in text
    assert "User Name: Asha Verma" in text
    assert serialize_request_public(req)["user_name"] == "Asha Verma"


def test_faculty_recharge_emails_name_the_faculty_with_prof(people, recharge_setup):
    from iic_booking.users.wallet_recharge_ops import sric_faculty_recharge_email_context
    from iic_booking.users.wallet_recharge_workflow import requester_details_text, serialize_request_public

    req = recharge_setup(people.faculty, WalletRechargeMode.PROJECT_GRANT)

    ctx = sric_faculty_recharge_email_context(RequestFactory().get("/"), req)
    assert ctx["faculty_name"] == ctx["faculty_display_name"] == ctx["user_name"] == "Prof. Ravi Kumar"
    assert "User Name: Prof. Ravi Kumar" in requester_details_text(req)
    assert serialize_request_public(req)["user_name"] == "Prof. Ravi Kumar"
    people.faculty.refresh_from_db()
    assert people.faculty.name == "Ravi Kumar"


def test_supervisor_invite_shows_the_typed_name_as_faculty(people):
    from iic_booking.users.supervisor_invites import serialize_invite

    invite = SimpleNamespace(
        id=1, email="new.prof@iitr.ac.in", supervisor_name="Meena Rao", department_id=None, department=None,
        message="", status="pending", get_status_display=lambda: "Pending", created_at=None, expires_at=None,
        last_sent_at=None, accepted_at=None, join_request_id=None,
    )
    data = serialize_invite(invite)
    assert data["supervisor_name"] == "Meena Rao"
    assert data["supervisor_display_name"] == "Prof. Meena Rao"
