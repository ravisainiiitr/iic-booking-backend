"""Linking a user to a verified I-STEM account also confirms their I-STEM portal registration, so the booking page
does not ask them to confirm it again by hand."""

from __future__ import annotations

import pytest

from iic_booking.istem import service
from iic_booking.istem.choices import UserLinkState
from iic_booking.istem.models import IstemUserLink
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def test_linked_user_is_marked_as_registered_on_istem():
    user = UserFactory(user_type=UserType.RND, istem_portal_acknowledged=False)
    link = IstemUserLink.objects.create(user=user)
    service._link_user(link, "60026", reason="test")
    user.refresh_from_db()
    assert link.state == UserLinkState.LINKED
    assert user.istem_portal_acknowledged is True


def test_a_link_held_for_review_does_not_confirm_registration():
    other = UserFactory(user_type=UserType.RND)
    IstemUserLink.objects.create(user=other, public_user_id="60026", state=UserLinkState.LINKED)
    user = UserFactory(user_type=UserType.RND, istem_portal_acknowledged=False)
    link = IstemUserLink.objects.create(user=user)
    service._link_user(link, "60026", reason="test")
    user.refresh_from_db()
    assert link.state == UserLinkState.NEEDS_REVIEW
    assert user.istem_portal_acknowledged is False
