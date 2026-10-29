"""The integration is off by default, and the admin keeps payloads from users without the sensitive permission."""

from __future__ import annotations

import pytest
from django.conf import settings as django_settings
from django.contrib.admin.sites import AdminSite
from django.test import RequestFactory

from iic_booking.istem.admin import (
    IstemApiAttemptAdmin,
    IstemBookingLinkAdmin,
    IstemSyncEventAdmin,
)
from iic_booking.istem.conf import istem_enabled, istem_mode
from iic_booking.istem.models import IstemApiAttempt, IstemBookingLink, IstemSyncEvent
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def test_integration_is_off_by_default():
    assert django_settings.ISTEM_ENABLED is False
    assert istem_enabled() is False
    assert istem_mode() == "off"


@pytest.mark.parametrize("mode", ["off", "shadow", "live"])
def test_mode_is_off_while_disabled(settings, mode):
    settings.ISTEM_ENABLED = False
    settings.ISTEM_MODE = mode

    assert istem_mode() == "off"


@pytest.mark.parametrize(("raw", "expected"), [("shadow", "shadow"), (" LIVE ", "live"), ("off", "off"), ("on", "off"), ("", "off")])
def test_mode_when_enabled(settings, raw, expected):
    settings.ISTEM_ENABLED = True
    settings.ISTEM_MODE = raw

    assert istem_mode() == expected


def _request(user):
    request = RequestFactory().get("/")
    request.user = user
    return request


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("admin_class", "model", "sensitive"),
    [
        (IstemBookingLinkAdmin, IstemBookingLink, {"create_payload"}),
        (IstemApiAttemptAdmin, IstemApiAttempt, {"request_redacted", "response_redacted"}),
    ],
)
def test_payload_fields_need_the_sensitive_permission(admin_class, model, sensitive):
    model_admin = admin_class(model, AdminSite())
    staff = UserFactory(user_type=UserType.ADMIN, is_staff=True, admin_approved=True)
    superuser = UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True, admin_approved=True)

    assert not sensitive & set(model_admin.get_fields(_request(staff)))
    assert sensitive <= set(model_admin.get_fields(_request(superuser)))


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("admin_class", "model"),
    [(IstemBookingLinkAdmin, IstemBookingLink), (IstemApiAttemptAdmin, IstemApiAttempt), (IstemSyncEventAdmin, IstemSyncEvent)],
)
def test_records_are_read_only_even_for_superusers(admin_class, model):
    model_admin = admin_class(model, AdminSite())
    request = _request(UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True, admin_approved=True))

    assert not model_admin.has_add_permission(request)
    assert not model_admin.has_change_permission(request)
    assert not model_admin.has_delete_permission(request)
