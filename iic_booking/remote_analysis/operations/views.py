"""API views for Operations Center."""

from __future__ import annotations

from django.http import FileResponse
from django.shortcuts import get_object_or_404
from django.utils.dateparse import parse_datetime
from pathlib import Path
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from django.conf import settings as django_settings

from iic_booking.remote_analysis.constants import AggregationPeriod, ReportFormat, ReportType
from iic_booking.remote_analysis.operations.alerts import AlertEngine
from iic_booking.remote_analysis.operations.analytics import AnalyticsEngine
from iic_booking.remote_analysis.operations.capacity import CapacityPlanner
from iic_booking.remote_analysis.operations.dashboards import OperationsDashboardService
from iic_booking.remote_analysis.operations.diagnostics import deployment_diagnostics
from iic_booking.remote_analysis.operations.commissioning import (
    commissioning_action,
    commissioning_console,
)
from iic_booking.remote_analysis.operations.toolkit_views import (
    toolkit_agent,
    toolkit_commissioning_report,
    toolkit_connectivity,
    toolkit_console,
    toolkit_dashboard,
    toolkit_fault_inject,
    toolkit_faults,
    toolkit_health_report,
    toolkit_live,
    toolkit_live_timeline,
    toolkit_logs,
    toolkit_monitoring_recommendations,
    toolkit_run_detail,
    toolkit_run_evidence,
    toolkit_run_failure_snapshots,
    toolkit_run_timeline,
    toolkit_runs,
    toolkit_self_test,
)
from iic_booking.remote_analysis.operations.performance import PerformanceMonitor
from iic_booking.remote_analysis.operations.reporting import ReportingEngine
from iic_booking.remote_analysis.operations.utilization import UtilizationEngine
from iic_booking.remote_analysis.operations_models import AlertEvent, AnalysisReport
from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis, CanViewRemoteAnalysis

_VIEW = [IsAuthenticated, CanViewRemoteAnalysis]
_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]


@api_view(["GET"])
@permission_classes(_VIEW)
def operations_dashboard(request):
    """GET /api/v1/analysis/operations/dashboard/"""
    refresh = request.query_params.get("refresh", "").lower() in {"1", "true", "yes"}
    if refresh and CanManageRemoteAnalysis().has_permission(request, None):
        payload = OperationsDashboardService().refresh_cache(actor=request.user)
    else:
        payload = OperationsDashboardService().build(refresh=refresh)
    return Response(payload)


@api_view(["GET"])
@permission_classes(_VIEW)
def analytics_view(request):
    """GET /api/v1/analysis/analytics/"""
    period = (request.query_params.get("period") or AggregationPeriod.DAILY).upper()
    return Response(AnalyticsEngine().analytics_payload(period))


@api_view(["GET"])
@permission_classes(_VIEW)
def utilization_view(request):
    """GET /api/v1/analysis/utilization/"""
    period = (request.query_params.get("period") or AggregationPeriod.DAILY).upper()
    if request.query_params.get("refresh") == "1":
        UtilizationEngine().aggregate(period)
    return Response(UtilizationEngine().summary(period))


@api_view(["GET"])
@permission_classes(_VIEW)
def performance_view(request):
    """GET /api/v1/analysis/performance/"""
    period = (request.query_params.get("period") or AggregationPeriod.HOURLY).upper()
    if request.query_params.get("refresh") == "1":
        return Response(PerformanceMonitor().aggregate(period))
    return Response(PerformanceMonitor().summary(period))


@api_view(["GET"])
@permission_classes(_VIEW)
def capacity_view(request):
    """GET /api/v1/analysis/capacity/"""
    period = (request.query_params.get("period") or AggregationPeriod.HOURLY).upper()
    if request.query_params.get("refresh") == "1":
        CapacityPlanner().snapshot(period)
    return Response(CapacityPlanner().summary(period))


@api_view(["GET"])
@permission_classes(_VIEW)
def alerts_list(request):
    """GET /api/v1/analysis/alerts/"""
    status_filter = request.query_params.get("status")
    alerts = AlertEngine().list_alerts(status=status_filter, limit=200)
    return Response(
        [
            {
                "id": str(a.id),
                "title": a.title,
                "message": a.message,
                "severity": a.severity,
                "category": a.category,
                "status": a.status,
                "acknowledged": a.acknowledged,
                "resolved": a.resolved,
                "workstation": getattr(a.workstation, "hostname", None),
                "assigned_to": getattr(a.assigned_to, "email", None),
                "created_at": a.created_at.isoformat(),
                "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
            }
            for a in alerts
        ]
    )


@api_view(["POST"])
@permission_classes(_MANAGE)
def alert_acknowledge(request, alert_id):
    """POST /api/v1/analysis/alerts/{id}/acknowledge/"""
    alert = get_object_or_404(AlertEvent, pk=alert_id)
    resolve = bool(request.data.get("resolve"))
    engine = AlertEngine()
    if resolve:
        alert = engine.resolve(alert, request.user)
    else:
        alert = engine.acknowledge(alert, request.user)
    return Response({"id": str(alert.id), "status": alert.status, "acknowledged": alert.acknowledged, "resolved": alert.resolved})


@api_view(["GET"])
@permission_classes(_VIEW)
def reports_list(request):
    """GET /api/v1/analysis/reports/"""
    qs = AnalysisReport.objects.order_by("-created_at")[:100]
    return Response(
        [
            {
                "id": str(r.id),
                "report_type": r.report_type,
                "format": r.format,
                "status": r.status,
                "created_at": r.created_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "has_file": bool(r.storage_relpath),
                "error_message": r.error_message,
            }
            for r in qs
        ]
    )


@api_view(["POST"])
@permission_classes(_MANAGE)
def reports_generate(request):
    """POST /api/v1/analysis/reports/generate/"""
    report_type = (request.data.get("report_type") or ReportType.DAILY_OPERATIONS).upper()
    fmt = (request.data.get("format") or ReportFormat.JSON).upper()
    if report_type not in {c.value for c in ReportType}:
        return Response({"detail": "Invalid report_type"}, status=status.HTTP_400_BAD_REQUEST)
    if fmt not in {c.value for c in ReportFormat}:
        return Response({"detail": "Invalid format"}, status=status.HTTP_400_BAD_REQUEST)
    start = parse_datetime(request.data.get("period_start") or "") if request.data.get("period_start") else None
    end = parse_datetime(request.data.get("period_end") or "") if request.data.get("period_end") else None
    report = ReportingEngine().generate(
        report_type,
        fmt=fmt,
        actor=request.user,
        period_start=start,
        period_end=end,
        parameters=request.data.get("parameters") or {},
    )
    return Response(
        {
            "id": str(report.id),
            "status": report.status,
            "report_type": report.report_type,
            "format": report.format,
            "payload": report.payload if report.format == ReportFormat.JSON else None,
            "error_message": report.error_message,
        },
        status=status.HTTP_201_CREATED if report.status != "FAILED" else status.HTTP_400_BAD_REQUEST,
    )


@api_view(["GET"])
@permission_classes(_VIEW)
def report_download(request, report_id):
    report = get_object_or_404(AnalysisReport, pk=report_id)
    if not report.storage_relpath:
        return Response(report.payload or {"detail": "No file"}, status=status.HTTP_200_OK)
    path = Path(django_settings.MEDIA_ROOT) / report.storage_relpath
    if not path.exists():
        return Response({"detail": "File missing"}, status=status.HTTP_404_NOT_FOUND)
    return FileResponse(open(path, "rb"), as_attachment=True, filename=path.name)
