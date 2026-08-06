"""Auth settings API: inactivity timeout is disabled; endpoint must not 500."""

from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


@pytest.mark.django_db
def test_auth_settings_admin_get_reports_timeout_disabled():
    admin = User.objects.create_superuser(
        email="sat.authsettings.admin@example.com",
        password="test-pass-12345",
        user_type=UserType.ADMIN,
        name="SAT Auth Settings Admin",
    )
    token = Token.objects.create(user=admin)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    response = client.get("/api/auth/settings/")
    assert response.status_code == 200
    data = response.json()
    assert data["inactivity_timeout_enabled"] is False
    assert data["single_session"] is True
    assert "CACHE_KEY" not in str(data)


@pytest.mark.django_db
def test_auth_settings_non_admin_forbidden():
    user = User.objects.create_user(
        email="sat.authsettings.faculty@example.com",
        password="test-pass-12345",
        user_type=UserType.FACULTY,
        email_verified=True,
        admin_approved=True,
        name="SAT Faculty",
    )
    user.is_active = True
    user.save(update_fields=["is_active"])
    token = Token.objects.create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")

    response = client.get("/api/auth/settings/")
    assert response.status_code == 403
