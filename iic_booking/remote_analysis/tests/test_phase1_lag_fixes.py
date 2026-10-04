"""Lag fixes: cached results listing, streamed RAW staging, bulk software match, viewport, disable-gfx."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from iic_booking.equipment.models import BookingResultFile
from iic_booking.equipment.remote_analysis_integration import raw_staging
from iic_booking.equipment.remote_analysis_integration.experience import AnalysisExperienceBuilder
from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
from iic_booking.remote_analysis.constants import ReservationStatus, WorkstationStatus
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.guacamole.connection import build_rdp_parameters, clamp_viewport
from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware
from iic_booking.remote_analysis.services.maintenance import MaintenanceService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _booking_for
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace_models import WorkspaceFile


@pytest.mark.parametrize(
    ("viewport", "expected"),
    [
        ({"width": 1280, "height": 720, "dpr": 2}, (2560, 1440)),
        ({"width": 1440, "height": 900, "dpr": 1}, (1440, 900)),
        ({"width": 800, "height": 500, "dpr": 1}, (1024, 700)),
        ({"width": 3000, "height": 2000, "dpr": 2}, (2560, 1600)),
        ({"width": 1365, "height": 767, "dpr": 1}, (1364, 766)),
        ({"width": 1280, "height": 720}, (1280, 720)),
        ({"width": 1100.6, "height": 801.2, "dpr": 1.25}, (1376, 1002)),
        ({"width": "x", "height": 720}, None),
        ({"width": 0, "height": 720, "dpr": 1}, None),
        (None, None),
        ([1280, 720], None),
    ],
)
def test_clamp_viewport(viewport, expected):
    assert clamp_viewport(viewport) == expected


def _session(ra_user, reservation_window, ra_settings, tmp_path, viewport=None):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    session = SessionOrchestrator().create_session(reservation=reservation, user=ra_user, viewport=viewport)
    return reservation, session


@pytest.mark.django_db
def test_create_session_applies_viewport_and_reuse_updates_it(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    reservation, session = _session(
        ra_user, reservation_window, ra_settings, tmp_path, viewport={"width": 1280, "height": 720, "dpr": 2}
    )
    assert (session.display_width, session.display_height) == (2560, 1440)

    again = SessionOrchestrator().create_session(
        reservation=reservation, user=ra_user, viewport={"width": 1600, "height": 900, "dpr": 1}
    )
    assert again.pk == session.pk
    again.refresh_from_db()
    assert (again.display_width, again.display_height) == (1600, 900)


@pytest.mark.django_db
def test_create_session_without_viewport_keeps_defaults(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    _, session = _session(ra_user, reservation_window, ra_settings, tmp_path)
    assert session.display_width == ra_settings.default_display_width
    assert session.display_height == ra_settings.default_display_height


@pytest.mark.django_db
def test_rdp_parameters_disable_gfx_toggle(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path, settings
):
    _, session = _session(ra_user, reservation_window, ra_settings, tmp_path)
    tunnel = SimpleNamespace(adapter_hostname="adapter.test", adapter_port=4822)

    settings.REMOTE_ANALYSIS_RDP_DISABLE_GFX = False
    params = build_rdp_parameters(session, ra_settings, tunnel=tunnel)
    assert "disable-gfx" not in params
    assert params["resize-method"] == ""

    settings.REMOTE_ANALYSIS_RDP_DISABLE_GFX = True
    params = build_rdp_parameters(session, ra_settings, tunnel=tunnel)
    assert params["disable-gfx"] == "true"


@pytest.mark.django_db
def test_has_raw_files_caches_s3_listing(ra_user):
    cache.clear()
    booking = _booking_for(ra_user)
    listing = [{"key": "Results/X/a.raw", "name": "a.raw", "size_bytes": 10, "source": "s3"}]
    with patch.object(raw_staging, "list_results_s3_objects", return_value=listing) as lister:
        svc = BookingRawStagingService()
        assert svc.has_raw_files(booking) is True
        assert svc.has_raw_files(booking) is True
        svc.list_raw_entries(booking)
    assert lister.call_count == 1


@pytest.mark.django_db
def test_has_raw_files_prefers_database_results(ra_user, settings, tmp_path):
    cache.clear()
    settings.MEDIA_ROOT = str(tmp_path)
    booking = _booking_for(ra_user)
    BookingResultFile.objects.create(
        booking=booking, file=ContentFile(b"spectrum", name="scan.raw"), original_name="scan.raw"
    )
    with patch.object(raw_staging, "list_results_s3_objects", return_value=[]) as lister:
        assert BookingRawStagingService().has_raw_files(booking) is True
    lister.assert_not_called()


@pytest.mark.django_db
def test_stage_streams_booking_result_files_and_is_idempotent(
    ra_user, eligible_workstation, reservation_window, ra_settings, settings, tmp_path
):
    cache.clear()
    settings.MEDIA_ROOT = str(tmp_path / "media")
    ra_settings.workspace_root = str(tmp_path / "ws")
    ra_settings.save()
    booking = _booking_for(ra_user)
    payload = b"0123456789" * 50_000
    BookingResultFile.objects.create(
        booking=booking, file=ContentFile(payload, name="big.raw"), original_name="big.raw"
    )
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user, booking=booking
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)

    with patch.object(raw_staging, "list_results_s3_objects", return_value=[]), patch.object(
        raw_staging, "CHUNK_BYTES", 4096
    ):
        first = BookingRawStagingService().stage_into_workspace(booking, workspace, actor=ra_user)
        second = BookingRawStagingService().stage_into_workspace(booking, workspace, actor=ra_user)

    assert first["staged"] == 1 and not first["errors"]
    assert second["staged"] == 0 and second["skipped"] == 1
    wf = WorkspaceFile.objects.get(workspace=workspace, relative_path="RawData/big.raw", is_current=True)
    assert wf.sha256.lower() == hashlib.sha256(payload).hexdigest()
    assert wf.size == len(payload)
    assert wf.source == "booking_raw"


@pytest.mark.django_db
def test_experience_get_does_not_expire_past_due_checkin(ra_user):
    booking = _booking_for(ra_user)
    ws = AnalysisWorkstation.objects.create(
        agent_id="ra-p1-checkin", hostname="P1", status=WorkstationStatus.RESERVED, enabled=True,
        health_score=100, last_heartbeat=timezone.now(),
    )
    now = timezone.now()
    reservation = AnalysisReservation.objects.create(
        user=ra_user, booking=booking, workstation=ws, status=ReservationStatus.AWAITING_CHECKIN,
        requested_start=now, requested_end=now + timedelta(hours=2), reserved_start=now,
        reserved_end=now + timedelta(hours=2), checkin_expires_at=now - timedelta(minutes=1), priority=100,
    )
    booking.analysis_reservation = reservation
    booking.save(update_fields=["analysis_reservation", "updated_at"])

    with patch("iic_booking.remote_analysis.services.checkin.CheckinService.expire_due") as expire:
        exp = AnalysisExperienceBuilder().build(booking)
    expire.assert_not_called()
    assert exp["awaiting_checkin"] is False
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.AWAITING_CHECKIN


@pytest.mark.django_db
def test_compatible_availability_software_match_is_one_query():
    statuses = [WorkstationStatus.OFFLINE, WorkstationStatus.AVAILABLE, WorkstationStatus.AVAILABLE, WorkstationStatus.AVAILABLE]
    workstations = [
        AnalysisWorkstation.objects.create(
            agent_id=f"ra-sw-{i}", hostname=f"SW-{i}", display_name=f"SW {i}", enabled=True, status=status
        )
        for i, status in enumerate(statuses)
    ]
    InstalledSoftware.objects.create(workstation=workstations[0], software_name="OriginPro 2024")
    InstalledSoftware.objects.create(workstation=workstations[0], software_name="MestReNova")
    InstalledSoftware.objects.create(workstation=workstations[1], software_name="originpro 2023")
    InstalledSoftware.objects.create(workstation=workstations[2], software_name="MestReNova")
    InstalledSoftware.objects.create(workstation=workstations[3], software_name="OriginPro", is_present=False)

    with CaptureQueriesContext(connection) as ctx:
        both = MaintenanceService().next_compatible_availability(required_software=["originpro", "mestrenova"])
    software_queries = [q for q in ctx.captured_queries if "installedsoftware" in q["sql"].lower()]
    assert len(software_queries) == 1
    # Only the OFFLINE PC has both packages.
    assert both["reason"] is not None or both.get("all_offline")

    either = MaintenanceService().next_compatible_availability(required_software=["ORIGINPRO"])
    assert either["reason"] is None and either["all_under_maintenance"] is False
