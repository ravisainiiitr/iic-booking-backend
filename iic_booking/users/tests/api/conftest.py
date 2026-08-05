"""Fixtures for OpenAPI documentation API tests."""

from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def openapi_admin_client(db):
    """
    Authenticate as staff/admin via production Token auth.

    OpenAPI views use DEFAULT_AUTHENTICATION_CLASSES (Token only) and
    SPECTACULAR_SETTINGS.SERVE_PERMISSIONS = IsAdminUser. Django session
    login (pytest-django admin_client) is intentionally not accepted.
    """
    user = UserFactory(
        user_type="admin",
        is_staff=True,
        is_superuser=True,
        admin_approved=True,
        email_verified=True,
    )
    token, _ = Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def openapi_anon_client():
    return APIClient()
