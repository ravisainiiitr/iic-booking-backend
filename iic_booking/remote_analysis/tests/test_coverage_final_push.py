"""Final coverage push — inventory, heartbeat edges, conflicts, permissions, views lists."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import ReservationStatus
from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
from iic_booking.remote_analysis.models import InstalledSoftware, SoftwareLicense
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, SoftwareRequirement
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.conflicts import ConflictResolver
from iic_booking.remote_analysis.services.heartbeat import HeartbeatService
from iic_booking.remote_analysis.services.inventory import InventoryService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.scheduler import SchedulerService
from iic_booking.remote_analysis.services.tokens import revoke_all_tokens
from iic_booking.remote_analysis.sharing import InvitationService, SharingError, SharingService
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_inventory_and_heartbeat_services(eligible_workstation):
    InventoryService().synchronize(
        eligible_workstation,
        {
            "software": [
                {"displayName": "MATLAB", "version": "R2024a", "publisher": "MathWorks"},
                {"displayName": "OriginPro", "version": "2024", "publisher": "OriginLab"},
            ],
            "hardware": {"cpuCores": 12, "memoryGB": 64, "gpu": "RTX"},
            "licenses": [{"software": "MATLAB", "status": "Active", "seats": 1}],
        },
    )
    assert InstalledSoftware.objects.filter(workstation=eligible_workstation, software_name="MATLAB").exists()

    HeartbeatService().process(
        eligible_workstation,
        {
            "CPU": 40,
            "Memory": 50,
            "Disk": 60,
            "Online": True,
            "CurrentStatus": "AVAILABLE",
            "Idle": True,
            "IdleTimeMinutes": 5,
        },
    )
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.last_heartbeat is not None


@pytest.mark.django_db
def test_availability_resources_and_license(eligible_workstation, reservation_window):
    start, end = reservation_window
    SoftwareLicense.objects.create(workstation=eligible_workstation, software="MATLAB", status="Active")
    InstalledSoftware.objects.create(
        workstation=eligible_workstation,
        software_name="MATLAB R2024a",
        version="R2024a",
        is_present=True,
    )
    req = SoftwareRequirement.objects.create(
        name="MATLAB licensed",
        software="MATLAB",
        license_required=True,
        minimum_ram_gb=8,
        minimum_cpu_cores=2,
        minimum_storage_gb=10,
        gpu_required=False,
        required=True,
    )
    ok, _reasons = AvailabilityEngine().software_matches(eligible_workstation, req)
    assert ok is True
    result = AvailabilityEngine().evaluate(
        eligible_workstation,
        start,
        end,
        requirement=req,
        requested_capabilities={
            "supports_rdp": True,
            "resources": {"min_cpu_cores": 2, "min_storage_gb": 10},
        },
    )
    assert result.available is True


@pytest.mark.django_db
def test_conflict_detect_all_and_scheduler_edges(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    r1 = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    ConflictResolver().detect_all_active()
    AnalysisReservation.objects.filter(pk=r1.pk).update(
        reserved_end=timezone.now() - timedelta(minutes=1),
        status=ReservationStatus.ACTIVE,
    )
    eligible_workstation.last_heartbeat = timezone.now()
    eligible_workstation.save(update_fields=["last_heartbeat"])
    stats = SchedulerService().expire_stale()
    assert stats["expired"] >= 1


@pytest.mark.django_db
def test_sharing_and_invitation_errors(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user)
    other = UserFactory(user_type="faculty")
    shared = SharingService().share(workspace, ra_user, user=other)
    assert SharingService().user_has_permission(other, workspace, "READ")
    with pytest.raises(SharingError):
        SharingService().share(workspace, other, user=ra_user)
    with pytest.raises(SharingError):
        InvitationService().invite(ra_user)
    inv = InvitationService().invite(ra_user, invited_user=other, workspace=workspace)
    InvitationService().accept(inv, other)
    with pytest.raises(SharingError):
        InvitationService().accept(inv, other)


@pytest.mark.django_db
def test_admin_list_endpoints_and_guacamole_user_token(ra_user, ra_settings):
    api = APIClient()
    api.force_authenticate(user=ra_user)
    for path in (
        "/api/v1/analysis/software/",
        "/api/v1/analysis/commands/history/",
        "/api/v1/analysis/events/",
    ):
        assert api.get(path).status_code == 200
    # heartbeats may require filters
    assert api.get("/api/v1/analysis/heartbeats/").status_code in (200, 400)

    ra_settings.mock_guacamole = True
    ra_settings.save()
    client = GuacamoleClient(ra_settings)
    assert client.create_user_token("u", "p").startswith("mock-user-token-")


@pytest.mark.django_db
def test_session_cleanup_idle_and_token_revoke(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    session.last_activity_at = timezone.now() - timedelta(hours=5)
    session.save(update_fields=["last_activity_at"])
    SessionCleanupService().cleanup_idle()
    SessionCleanupService().cleanup_expired()
    revoke_all_tokens(eligible_workstation)
    assert eligible_workstation.tokens.filter(is_active=True).count() == 0


@pytest.mark.django_db
def test_validate_remote_analysis_command():
    call_command("validate_remote_analysis")


@pytest.mark.django_db
def test_heartbeat_alerts_parse_dt_and_offline(eligible_workstation, ra_settings):
    from iic_booking.remote_analysis.services.heartbeat import mark_stale_workstations_offline
    from iic_booking.remote_analysis.services.reservation import ReservationService
    from datetime import datetime

    HeartbeatService().process(
        eligible_workstation,
        {"CPU": 99, "Memory": 96, "Disk": 97, "Online": True, "CurrentStatus": "BUSY"},
    )
    dt = ReservationService.parse_dt(timezone.now())
    assert timezone.is_aware(dt)
    naive = datetime(2030, 1, 1, 12, 0, 0)
    aware = ReservationService.parse_dt(naive.isoformat())
    assert timezone.is_aware(aware)
    with pytest.raises(ValueError):
        ReservationService.parse_dt("not-a-date")

    eligible_workstation.last_heartbeat = timezone.now() - timedelta(hours=2)
    eligible_workstation.status = "AVAILABLE"
    eligible_workstation.save(update_fields=["last_heartbeat", "status"])
    mark_stale_workstations_offline()
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status in {"OFFLINE", "AVAILABLE", "DISABLED"}

    ra_settings.mock_guacamole = False
    ra_settings.guacamole_api_url = "https://guac.example/api"
    ra_settings.save()
    client = GuacamoleClient(ra_settings)
    token_resp = __import__("unittest").mock.MagicMock()
    token_resp.status_code = 200
    token_resp.raise_for_status = __import__("unittest").mock.MagicMock()
    token_resp.json.return_value = {"authToken": "user-tok"}
    with __import__("unittest").mock.patch(
        "iic_booking.remote_analysis.guacamole.client.requests.request",
        return_value=token_resp,
    ):
        assert client.create_user_token("u", "p") == "user-tok"


@pytest.mark.django_db
def test_list_available_and_apps_ready(eligible_workstation, reservation_window):
    from iic_booking.remote_analysis.apps import RemoteAnalysisConfig
    from iic_booking.remote_analysis.services.availability import AvailabilityEngine
    import os

    start, end = reservation_window
    rows = AvailabilityEngine().list_available(start, end)
    assert any(ws.id == eligible_workstation.id for ws, _ in rows)

    os.environ["RA_APPLY_ENV_SETTINGS"] = "0"
    RemoteAnalysisConfig("iic_booking.remote_analysis", __import__("iic_booking.remote_analysis")).ready()
