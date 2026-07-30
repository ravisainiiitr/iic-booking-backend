"""Reservation + session API smoke tests (WS3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import ReservationStatus
from iic_booking.remote_analysis.services.reservation import ReservationService


@pytest.fixture
def api(ra_user):
    client = APIClient()
    client.force_authenticate(user=ra_user)
    return client


@pytest.mark.django_db
def test_create_reservation_api(api, eligible_workstation, reservation_window):
    start, end = reservation_window
    response = api.post(
        "/api/v1/analysis/reservations/",
        {
            "requested_start": start.isoformat(),
            "requested_end": end.isoformat(),
            "priority": 100,
        },
        format="json",
    )
    assert response.status_code in (200, 201)
    body = response.json()
    assert body["status"] in {ReservationStatus.RESERVED, ReservationStatus.QUEUED}
    assert body["id"]


@pytest.mark.django_db
def test_list_reservations_api(api, ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    response = api.get("/api/v1/analysis/reservations/")
    assert response.status_code == 200
    assert len(response.json()) >= 1


@pytest.mark.django_db
def test_cancel_reservation_api(api, ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    response = api.post(f"/api/v1/analysis/reservations/{reservation.id}/cancel/")
    assert response.status_code == 200
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.CANCELLED


@pytest.mark.django_db
def test_extend_reservation_api(api, ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    new_end = end + timedelta(hours=1)
    response = api.post(
        f"/api/v1/analysis/reservations/{reservation.id}/extend/",
        {"new_end": new_end.isoformat()},
        format="json",
    )
    assert response.status_code == 200
    reservation.refresh_from_db()
    assert reservation.reserved_end == new_end


@pytest.mark.django_db
def test_availability_api(api, eligible_workstation, reservation_window):
    start, end = reservation_window
    response = api.get(
        "/api/v1/analysis/availability/",
        {"start": start.isoformat(), "end": end.isoformat()},
    )
    assert response.status_code == 200
    body = response.json()
    assert isinstance(body, (list, dict))


@pytest.mark.django_db
def test_candidates_api(api, eligible_workstation, reservation_window):
    start, end = reservation_window
    response = api.get(
        "/api/v1/analysis/candidates/",
        {"start": start.isoformat(), "end": end.isoformat()},
    )
    assert response.status_code == 200


@pytest.mark.django_db
def test_session_create_launch_terminate_api(
    api, ra_user, eligible_workstation, reservation_window, ra_settings
):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
    )
    create = api.post(
        "/api/v1/analysis/session/create/",
        {"reservation_id": str(reservation.id)},
        format="json",
    )
    assert create.status_code in (200, 201)
    session_id = create.json()["id"]

    launch = api.get(f"/api/v1/analysis/session/{session_id}/launch/")
    assert launch.status_code == 200
    assert "launch_url" in launch.json() or "session_id" in launch.json()

    status_resp = api.get(f"/api/v1/analysis/session/{session_id}/status/")
    assert status_resp.status_code == 200

    terminate = api.post(
        f"/api/v1/analysis/session/{session_id}/terminate/",
        {"reason": "api test"},
        format="json",
    )
    assert terminate.status_code == 200


@pytest.mark.django_db
def test_scheduler_status_api(api):
    response = api.get("/api/v1/analysis/scheduler/status/")
    assert response.status_code == 200
