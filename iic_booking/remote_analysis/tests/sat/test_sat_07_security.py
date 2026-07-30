"""SAT-07 Security."""

from __future__ import annotations

import pytest
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_07_01_anonymous_json_denied(sat_anon, settings):
    settings.FRONTEND_URL = "https://portal.example"
    res = sat_anon.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code in {401, 403}
    assert "detail" in res.json()


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_07_01_anonymous_html_redirects(sat_anon, settings):
    settings.FRONTEND_URL = "https://portal.example"
    res = sat_anon.get("/api/v1/analysis/operations/commissioning/?view=html")
    assert res.status_code == 302
    assert res["Location"].startswith("https://portal.example/login?")


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_07_02_normal_user_denied():
    user = UserFactory(
        user_type=UserType.STUDENT,
        admin_approved=True,
        email_verified=True,
    )
    api = APIClient()
    api.force_authenticate(user=user)
    res = api.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code == 403


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_07_05_super_admin_allowed(sat_api):
    res = sat_api.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code == 200


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_07_07_invalid_agent_token():
    api = APIClient()
    res = api.post(
        "/api/v1/analysis/heartbeat/",
        {"cpuPercent": 1},
        format="json",
        HTTP_AUTHORIZATION="Bearer totally-invalid-token",
        HTTP_X_AGENT_ID="nope",
    )
    assert res.status_code in {401, 403}


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_07_10_query_token_ignored_for_json(sat_admin):
    token, _ = Token.objects.get_or_create(user=sat_admin)
    api = APIClient()
    res = api.get(f"/api/v1/analysis/operations/commissioning/?token={token.key}")
    assert res.status_code in {401, 403}


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_07_08_session_post_requires_csrf(sat_admin):
    api = APIClient(enforce_csrf_checks=True)
    api.force_login(sat_admin)
    res = api.post(
        "/api/v1/analysis/operations/commissioning/action/",
        {"action": "refresh"},
        format="json",
    )
    assert res.status_code in {403, 401}


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_07_09_session_hijack_lab(sat_lab_enabled):
    pytest.skip("Lab: stolen session cookie from other origin must not grant manage access.")
