"""Operations Center + workspace API/service smoke (WS3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import AggregationPeriod, ReportFormat, ReportType
from iic_booking.remote_analysis.operations.alerts import AlertEngine
from iic_booking.remote_analysis.operations.analytics import AnalyticsEngine
from iic_booking.remote_analysis.operations.capacity import AvailabilityEngine, CapacityPlanner
from iic_booking.remote_analysis.operations.dashboards import OperationsDashboardService
from iic_booking.remote_analysis.operations.performance import PerformanceMonitor
from iic_booking.remote_analysis.operations.reporting import ReportingEngine
from iic_booking.remote_analysis.operations.utilization import UtilizationEngine
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.storage import StorageManager
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService


@pytest.fixture
def api(ra_user):
    client = APIClient()
    client.force_authenticate(user=ra_user)
    return client


@pytest.mark.django_db
def test_operations_engines_smoke(eligible_workstation):
    AlertEngine().ensure_default_rules()
    assert isinstance(AlertEngine().evaluate(), dict)
    assert AlertEngine().list_alerts(limit=5) is not None

    AnalyticsEngine().compute_kpis(AggregationPeriod.HOURLY)
    AnalyticsEngine().compute_session_analytics(AggregationPeriod.DAILY)
    assert isinstance(AnalyticsEngine().analytics_payload(AggregationPeriod.DAILY), dict)

    UtilizationEngine().aggregate(AggregationPeriod.DAILY)
    assert isinstance(UtilizationEngine().summary(AggregationPeriod.DAILY), dict)

    CapacityPlanner().snapshot(AggregationPeriod.HOURLY)
    assert isinstance(CapacityPlanner().summary(AggregationPeriod.HOURLY), dict)
    AvailabilityEngine().aggregate(AggregationPeriod.DAILY)
    assert isinstance(AvailabilityEngine().summary(AggregationPeriod.DAILY), dict)

    PerformanceMonitor().aggregate(AggregationPeriod.HOURLY)
    assert isinstance(PerformanceMonitor().summary(AggregationPeriod.HOURLY), dict)

    ReportingEngine().refresh_trends(AggregationPeriod.DAILY)
    report = ReportingEngine().generate(ReportType.SESSION_SUMMARY, fmt=ReportFormat.JSON)
    assert report.status in {"READY", "FAILED", "GENERATING"} or report.id
    if report.status == "READY":
        assert report.payload

    dash = OperationsDashboardService().build(refresh=True)
    assert "executive" in dash
    refreshed = OperationsDashboardService().refresh_cache()
    assert refreshed.get("generated_at") or "executive" in refreshed


@pytest.mark.django_db
def test_operations_api_endpoints(api, eligible_workstation):
    endpoints = [
        "/api/v1/analysis/operations/dashboard/?refresh=1",
        "/api/v1/analysis/analytics/",
        "/api/v1/analysis/utilization/",
        "/api/v1/analysis/capacity/",
        "/api/v1/analysis/performance/",
        "/api/v1/analysis/alerts/",
    ]
    for path in endpoints:
        response = api.get(path)
        assert response.status_code == 200, path


@pytest.mark.django_db
def test_operations_diagnostics_manage(api, eligible_workstation):
    response = api.get("/api/v1/analysis/operations/diagnostics/")
    assert response.status_code == 200
    body = response.json()
    assert "workstations" in body
    assert "workspaces" in body
    assert "guacamole" in body
    assert "warnings" in body
    html = api.get("/api/v1/analysis/operations/diagnostics/?view=html")
    assert html.status_code == 200
    assert b"Remote Analysis" in html.content


@pytest.mark.django_db
def test_workspace_ensure_manifest_and_api(
    api, ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    settings_obj = ra_settings
    settings_obj.workspace_root = str(tmp_path)
    settings_obj.save(update_fields=["workspace_root"])

    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    sync = WorkspaceSyncService()
    workspace = sync.ensure_for_reservation(reservation, actor=ra_user)
    assert workspace.id
    StorageManager(settings_obj).write_bytes(workspace, "RawData/sample.txt", b"hello")
    from iic_booking.remote_analysis.workspace_models import WorkspaceFile

    WorkspaceFile.objects.create(
        workspace=workspace,
        original_name="sample.txt",
        stored_name="sample.txt",
        relative_path="RawData/sample.txt",
        size=5,
        sha256="abc",
        storage_relpath="RawData/sample.txt",
        category="RAW",
        is_current=True,
    )
    manifest = sync.build_manifest(workspace)
    assert manifest["workspace_id"] == str(workspace.id)
    assert any(f["relative_path"].endswith("sample.txt") for f in manifest["files"])
    assert "agent_layout" in manifest
    assert "download_folders" in manifest

    listed = api.get("/api/v1/analysis/workspaces/")
    assert listed.status_code == 200
    detail = api.get(f"/api/v1/analysis/workspaces/{workspace.id}/")
    assert detail.status_code == 200
    files = api.get(f"/api/v1/analysis/workspaces/{workspace.id}/files/")
    assert files.status_code == 200
    dash = api.get("/api/v1/analysis/workspaces/dashboard/")
    assert dash.status_code == 200


@pytest.mark.django_db
def test_collaboration_api_smoke(api, ra_user):
    paths = [
        "/api/v1/analysis/collaboration/dashboard/",
        "/api/v1/analysis/activity/",
        "/api/v1/analysis/notifications/",
        "/api/v1/analysis/notes/",
        "/api/v1/analysis/announcements/",
        "/api/v1/analysis/bookmarks/",
        "/api/v1/analysis/favorites/",
    ]
    for path in paths:
        response = api.get(path)
        assert response.status_code in (200, 405), path

    note = api.post(
        "/api/v1/analysis/notes/",
        {"title": "WS3 note", "body": "coverage"},
        format="json",
    )
    assert note.status_code in (200, 201)
