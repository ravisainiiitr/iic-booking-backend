"""Phase R.2.6 — version manifest, capabilities, installer compatibility."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.platform_compat.semver import compare_installer, parse_version, version_gte
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def admin_client(db):
    user = User.objects.create_superuser(
        email="r26-admin@example.com",
        password="test-pass-12345",
        user_type=UserType.ADMIN,
        name="R26 Admin",
    )
    Token.objects.get_or_create(user=user)
    client = APIClient()
    token = Token.objects.get(user=user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def anon_client():
    return APIClient()


def test_parse_version_loose():
    assert parse_version("1.0.1") == (1, 0, 1)
    assert parse_version("v2.5.2-r2")[:3] == (2, 5, 2)
    assert version_gte("1.0.2", "1.0.1")
    assert not version_gte("1.0.0", "1.0.1")


def test_compare_installer_matrix():
    supported = {
        "dsa": {"minimum": "1.0.1", "latest": "1.0.2"},
    }
    old = compare_installer("dsa", "1.0.0", supported)
    assert old["compatible"] is False
    assert old["status"] == "unsupported"

    mid = compare_installer("dsa", "1.0.1", supported)
    assert mid["compatible"] is True
    assert mid["status"] == "upgrade_recommended"

    new = compare_installer("dsa", "1.0.2", supported)
    assert new["compatible"] is True
    assert new["status"] == "compatible"


def test_urlconf_loads_with_research_copilot_app(settings):
    """Research Copilot is installed; APIs remain feature-flag gated."""
    assert any("research_copilot" in str(a) for a in settings.INSTALLED_APPS)
    from django.urls import clear_url_caches, get_resolver, resolve

    clear_url_caches()
    assert get_resolver().url_patterns
    assert resolve("/api/version/").url_name == "api-version"
    assert resolve("/api/v1/provisioning/capabilities/").url_name == "capabilities"
    assert resolve("/api/v1/research-copilot/bootstrap/").url_name == "bootstrap"


@pytest.mark.django_db
def test_api_version_public(anon_client, settings):
    settings.PORTAL_VERSION = "2.5.2"
    settings.BACKEND_GIT_COMMIT = "abc1234"
    settings.FRONTEND_VERSION = "2.5.2-r2"
    settings.PROVISIONING_VERSION = "2.0"
    resp = anon_client.get("/api/version/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["portal_version"] == "2.5.2"
    assert body["backend_commit"] == "abc1234"
    assert body["frontend_version"] == "2.5.2-r2"
    assert body["provisioning_version"] == "2.0"
    assert "dsa" in body["supported_installers"]
    assert body["supported_installers"]["dsa"]["minimum"] == "1.0.1"


@pytest.mark.django_db
def test_capabilities_public(anon_client, settings):
    settings.PROVISIONING_ENABLED = True
    settings.RESEARCH_COPILOT_ENABLED = False
    resp = anon_client.get("/api/v1/provisioning/capabilities/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["zero_touch"] is True
    assert body["installer_auth"] is True
    assert body["auto_approve"] is True
    assert body["device_code"] is True
    assert body["research_copilot"] is False
    assert body["links"]["installer_auth"] == "/device-provisioning/installer-auth"


@pytest.mark.django_db
def test_capabilities_installer_too_old(anon_client):
    resp = anon_client.get(
        "/api/v1/provisioning/capabilities/",
        {"product": "dsa", "installer_version": "1.0.0"},
    )
    assert resp.status_code == 200
    compat = resp.json()["installer_compatibility"]
    assert compat["compatible"] is False
    assert compat["traffic_light"] == "red"


@pytest.mark.django_db
def test_capabilities_when_provisioning_disabled(anon_client, settings):
    settings.PROVISIONING_ENABLED = False
    resp = anon_client.get("/api/v1/provisioning/capabilities/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["zero_touch"] is False
    assert body["installer_auth"] is False
    assert body["provisioning_enabled"] is False


@pytest.mark.django_db
def test_self_test_requires_admin(anon_client, admin_client, settings):
    settings.FRONTEND_VERSION = "2.5.2-r2"
    settings.COMPATIBLE_FRONTEND_MIN = "2.5.2-r2"
    settings.BACKEND_GIT_COMMIT = "deadbeef"
    assert anon_client.get("/api/v1/provisioning/self-test/").status_code in (401, 403)
    resp = admin_client.get(
        "/api/v1/provisioning/self-test/",
        {"product": "dsa", "installer_version": "1.0.2"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["overall"] in ("PASS", "FAIL")
    assert any(c["name"] == "installer_auth" for c in body["checks"])
