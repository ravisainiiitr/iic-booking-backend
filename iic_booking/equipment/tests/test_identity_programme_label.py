"""Identity card programme: the degree's long expansion is dropped and the degree is not repeated in the branch."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.api_views import _identity_programme_label, _programme_label
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.parametrize(
    ("degree", "branch", "expected"),
    [
        ("Ph.D. - Doctor of Philosophy", "Ph.D. Physics", "Ph.D. Physics"),
        ("Ph.D. - Doctor of Philosophy", "Physics", "Ph.D. Physics"),
        ("PhD - Doctor of Philosophy", "Ph.D. Chemistry", "Ph.D. Chemistry"),
        ("M.Tech. - Master of Technology", "Computer Science (M.Tech.)", "Computer Science (M.Tech.)"),
        ("M.Tech", "Chemical Engineering", "M.Tech Chemical Engineering"),
        ("MA - Master of Arts", "Mathematics", "MA Mathematics"),
        ("Ph.D. – Doctor of Philosophy", "", "Ph.D."),
        ("", "Ph.D. Physics", "Ph.D. Physics"),
        ("", "", ""),
    ],
)
def test_programme_label(degree, branch, expected):
    assert _programme_label(degree, branch) == expected


def test_identity_programme_label_falls_back_to_designation():
    target = SimpleNamespace(degree_name="", branch_name="", designation="Professor")
    assert _identity_programme_label(target) == "Professor"


@pytest.mark.django_db
def test_identity_card_shows_deduplicated_programme():
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    student = UserFactory(
        admin_approved=True,
        user_type=UserType.STUDENT,
        degree_name="Ph.D. - Doctor of Philosophy",
        branch_name="Ph.D. Physics",
    )
    supervisor = UserFactory(admin_approved=True, user_type=UserType.FACULTY)
    WalletJoinRequest.objects.create(
        student=student,
        faculty=supervisor,
        wallet=Wallet.objects.create(user=supervisor),
        status=WalletJoinRequestStatus.APPROVED,
    )
    client = APIClient()
    client.force_authenticate(user=supervisor)
    with patch("iic_booking.users.rbac.user_has_permission", return_value=False):
        res = client.get(f"/api/staff/users/{student.pk}/identity-card/")
    assert res.status_code == 200, res.data
    assert res.data["programme"] == "Ph.D. Physics"
