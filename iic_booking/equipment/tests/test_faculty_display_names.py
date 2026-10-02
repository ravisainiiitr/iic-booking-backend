"""Booking and identity-card APIs show IITR faculty as "Prof. <name>"; students keep their plain name."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.serializers import _get_wallet_owner_display_name
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.factories import UserFactory


def _supervised_student(faculty_name):
    faculty = UserFactory(admin_approved=True, user_type=UserType.FACULTY, name=faculty_name)
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT, name="Asha Verma")
    WalletJoinRequest.objects.create(
        student=student,
        faculty=faculty,
        wallet=Wallet.objects.create(user=faculty),
        status=WalletJoinRequestStatus.APPROVED,
    )
    return student, faculty


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("faculty_name", "expected"),
    [
        ("Ravi Kumar", "Prof. Ravi Kumar"),
        ("Prof. Ravi Kumar", "Prof. Ravi Kumar"),
        ("Dr. Ravi Kumar", "Dr. Ravi Kumar"),
    ],
)
def test_booking_supervisor_name_has_prof(faculty_name, expected):
    student, faculty = _supervised_student(faculty_name)
    assert _get_wallet_owner_display_name(student, {}) == expected
    assert _get_wallet_owner_display_name(faculty, {}) is None
    faculty.refresh_from_db()
    assert faculty.name == faculty_name


@pytest.mark.django_db
def test_identity_card_prefixes_supervisor_but_not_student():
    student, faculty = _supervised_student("Ravi Kumar")
    client = APIClient()
    client.force_authenticate(user=faculty)
    with patch("iic_booking.users.rbac.user_has_permission", return_value=False):
        res = client.get(f"/api/staff/users/{student.pk}/identity-card/")
    assert res.status_code == 200, res.data
    assert res.data["supervisor_name"] == "Prof. Ravi Kumar"
    assert res.data["name"] == "Asha Verma"


@pytest.mark.django_db
def test_identity_card_of_faculty_shows_prof():
    faculty = UserFactory(admin_approved=True, user_type=UserType.FACULTY, name="Meena Rao")
    staff = UserFactory(admin_approved=True, user_type=UserType.ADMIN, name="Office Staff")
    client = APIClient()
    client.force_authenticate(user=staff)
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = client.get(f"/api/staff/users/{faculty.pk}/identity-card/")
    assert res.status_code == 200, res.data
    assert res.data["name"] == "Prof. Meena Rao"
