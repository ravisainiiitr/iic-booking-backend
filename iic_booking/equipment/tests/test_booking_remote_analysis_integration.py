"""Tests for Booking ↔ Remote Analysis integration layer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.remote_analysis_integration.eligibility import BookingAnalysisEligibilityService
from iic_booking.users.tests.factories import UserFactory


def test_eligibility_disabled_when_equipment_flag_off():
    equipment = SimpleNamespace(
        enable_remote_analysis=False,
        remote_analysis_enabled_from_status="COMPLETED",
        analysis_requires_experiment_completion=True,
        analysis_requires_sample_acceptance=False,
        analysis_session_limit=5,
    )
    booking = SimpleNamespace(
        equipment=equipment,
        status=BookingStatus.COMPLETED,
        analysis_expiry=None,
        analysis_session_count=0,
    )
    result = BookingAnalysisEligibilityService().evaluate(booking)
    assert result.eligible is False
    assert "not enabled" in result.reason.lower()


def test_eligibility_ok_when_completed_and_enabled():
    equipment = SimpleNamespace(
        enable_remote_analysis=True,
        remote_analysis_enabled_from_status="COMPLETED",
        analysis_requires_experiment_completion=True,
        analysis_requires_sample_acceptance=False,
        analysis_session_limit=5,
    )
    booking = SimpleNamespace(
        equipment=equipment,
        status=BookingStatus.COMPLETED,
        analysis_expiry=None,
        analysis_session_count=0,
    )
    result = BookingAnalysisEligibilityService().evaluate(booking)
    assert result.eligible is True


@pytest.mark.django_db
def test_analysis_api_requires_auth(client):
    response = client.get("/api/v1/bookings/1/analysis/")
    assert response.status_code in (401, 403)


@pytest.mark.django_db
def test_dashboard_endpoint_authenticated():
    from rest_framework.test import APIClient

    user = UserFactory()
    client = APIClient()
    client.force_authenticate(user=user)
    response = client.get("/api/v1/bookings/analysis/dashboard/?scope=user")
    assert response.status_code == 200
    body = response.json()
    assert "ready" in body
