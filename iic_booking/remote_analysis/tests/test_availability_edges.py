"""Availability / allocation edge-case tests (WS3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis.constants import ReservationStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware, MaintenanceWindow
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, SoftwareRequirement
# MaintenanceWindow is re-exported from models (scheduler_models)
from iic_booking.remote_analysis.services.allocation import AllocationService
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_evaluate_maintenance_blocks(eligible_workstation, reservation_window):
    start, end = reservation_window
    MaintenanceWindow.objects.create(
        workstation=eligible_workstation,
        reason="Patching",
        start=start - timedelta(minutes=10),
        end=end + timedelta(minutes=10),
        active=True,
    )
    result = AvailabilityEngine().evaluate(eligible_workstation, start, end)
    assert result.available is False
    assert any("Maintenance" in r for r in result.reasons)


@pytest.mark.django_db
def test_evaluate_reservation_overlap(eligible_workstation, reservation_window, ra_user):
    start, end = reservation_window
    AnalysisReservation.objects.create(
        user=ra_user,
        workstation=eligible_workstation,
        status=ReservationStatus.RESERVED,
        requested_start=start,
        requested_end=end,
        reserved_start=start,
        reserved_end=end,
        priority=100,
    )
    result = AvailabilityEngine().evaluate(eligible_workstation, start, end)
    assert result.available is False
    assert any("overlap" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_evaluate_stale_heartbeat_soft_online(db, reservation_window):
    """AVAILABLE + enabled + valid token remains allocatable despite stale heartbeat."""
    start, end = reservation_window
    ws = AnalysisWorkstation.objects.create(
        agent_id="stale-hb",
        hostname="STALE",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now() - timedelta(minutes=30),
    )
    issue_agent_token(ws)
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is True, result.reasons
    assert result.reasons == []


@pytest.mark.django_db
def test_evaluate_stale_heartbeat_online_status_soft_online(db, reservation_window):
    """ONLINE status also uses soft-online when heartbeat is stale."""
    start, end = reservation_window
    ws = AnalysisWorkstation.objects.create(
        agent_id="stale-hb-online",
        hostname="STALE-ONLINE",
        status=WorkstationStatus.ONLINE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now() - timedelta(minutes=30),
    )
    issue_agent_token(ws)
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is True, result.reasons


@pytest.mark.django_db
def test_evaluate_stale_heartbeat_without_token(db, reservation_window):
    """Stale heartbeat without a usable agent token is not allocatable."""
    start, end = reservation_window
    ws = AnalysisWorkstation.objects.create(
        agent_id="stale-hb-no-token",
        hostname="STALE-NO-TOKEN",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now() - timedelta(minutes=30),
    )
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is False
    assert any("heartbeat" in r.lower() or "offline" in r.lower() for r in result.reasons)
    assert any("token" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_evaluate_stale_heartbeat_disabled(db, reservation_window):
    """Disabled workstations stay unavailable even with soft-online inputs."""
    start, end = reservation_window
    ws = AnalysisWorkstation.objects.create(
        agent_id="stale-hb-disabled",
        hostname="STALE-DISABLED",
        status=WorkstationStatus.AVAILABLE,
        enabled=False,
        health_score=90,
        last_heartbeat=timezone.now() - timedelta(minutes=30),
    )
    issue_agent_token(ws)
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is False
    assert any("disabled" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_evaluate_stale_heartbeat_offline_status(db, reservation_window):
    """OFFLINE status is not allocatable regardless of token or heartbeat age."""
    start, end = reservation_window
    ws = AnalysisWorkstation.objects.create(
        agent_id="stale-hb-offline",
        hostname="STALE-OFFLINE",
        status=WorkstationStatus.OFFLINE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now() - timedelta(minutes=30),
    )
    issue_agent_token(ws)
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is False
    assert any("not allocatable" in r.lower() or "offline" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_evaluate_stale_heartbeat_expired_token(db, reservation_window):
    """Expired agent token blocks soft-online allocation."""
    start, end = reservation_window
    ws = AnalysisWorkstation.objects.create(
        agent_id="stale-hb-expired-token",
        hostname="STALE-EXPIRED",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now() - timedelta(minutes=30),
    )
    token, _ = issue_agent_token(ws)
    token.expires_at = timezone.now() - timedelta(minutes=1)
    token.save(update_fields=["expires_at"])
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is False
    assert any("token" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_evaluate_missing_token(db, reservation_window):
    start, end = reservation_window
    ws = AnalysisWorkstation.objects.create(
        agent_id="no-token",
        hostname="NOTOKEN",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now(),
    )
    result = AvailabilityEngine().evaluate(ws, start, end)
    assert result.available is False
    assert any("token" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_software_matches_required_and_version(eligible_workstation):
    InstalledSoftware.objects.create(
        workstation=eligible_workstation,
        software_name="OriginPro",
        version="2023",
        publisher="OriginLab",
        is_present=True,
    )
    engine = AvailabilityEngine()
    missing = SoftwareRequirement.objects.create(name="Need MATLAB", software="MATLAB", required=True)
    ok, reasons = engine.software_matches(eligible_workstation, missing)
    assert ok is False
    assert reasons

    present = SoftwareRequirement.objects.create(name="Need Origin", software="Origin", required=True)
    ok2, reasons2 = engine.software_matches(eligible_workstation, present)
    assert ok2 is True
    assert reasons2 == []

    versioned = SoftwareRequirement.objects.create(
        name="Origin new",
        software="Origin",
        minimum_version="2025",
        required=True,
    )
    ok3, reasons3 = engine.software_matches(eligible_workstation, versioned)
    assert ok3 is False
    assert reasons3


@pytest.mark.django_db
def test_capability_matches_rdp_and_resources(eligible_workstation):
    engine = AvailabilityEngine()
    ok, reasons = engine.capability_matches(
        eligible_workstation,
        {"supports_rdp": True, "resources": {"min_ram_gb": 128}},
    )
    assert ok is False
    assert any("RAM" in r or "ram" in r.lower() for r in reasons)


@pytest.mark.django_db
def test_rank_candidates_orders_by_score(reservation_window):
    start, end = reservation_window
    low = AnalysisWorkstation.objects.create(
        agent_id="rank-low",
        hostname="LOW",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=55,
        last_heartbeat=timezone.now(),
        supports_rdp=True,
    )
    high = AnalysisWorkstation.objects.create(
        agent_id="rank-high",
        hostname="HIGH",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=99,
        last_heartbeat=timezone.now(),
        supports_rdp=True,
    )
    issue_agent_token(low)
    issue_agent_token(high)
    ranked = AllocationService().rank_candidates(start=start, end=end)
    assert ranked
    assert ranked[0].workstation.id == high.id


@pytest.mark.django_db
def test_select_best_none_when_empty(reservation_window):
    start, end = reservation_window
    assert AllocationService().select_best(start=start, end=end) is None


@pytest.mark.django_db
def test_unavailable_score_is_zero(eligible_workstation, reservation_window):
    start, end = reservation_window
    eligible_workstation.status = WorkstationStatus.OFFLINE
    eligible_workstation.save(update_fields=["status"])
    scored = AllocationService().score_workstation(eligible_workstation, start=start, end=end)
    assert scored.available is False
    assert scored.score == 0.0
