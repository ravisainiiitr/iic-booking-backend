"""Celery background jobs for the Remote Analysis scheduler."""

from __future__ import annotations

import logging

from iic_booking.remote_analysis.production_hardening import ra_periodic_task

logger = logging.getLogger(__name__)


@ra_periodic_task(name="remote_analysis.expire_reservations")
def expire_reservations() -> dict:
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    result = SchedulerService().expire_stale()
    logger.info("expire_reservations: %s", result)
    return result


@ra_periodic_task(name="remote_analysis.process_reservation_queue")
def process_reservation_queue(limit: int = 20) -> dict:
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    result = SchedulerService().process_queue(limit=limit)
    logger.info("process_reservation_queue: %s", result)
    return result


@ra_periodic_task(name="remote_analysis.refresh_workstation_health")
def refresh_workstation_health() -> int:
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    count = SchedulerService().refresh_health()
    logger.info("refresh_workstation_health: %s", count)
    return count


@ra_periodic_task(name="remote_analysis.monitor_maintenance_windows")
def monitor_maintenance_windows() -> dict:
    from django.utils import timezone

    from iic_booking.remote_analysis.constants import AuditCategory, WorkstationStatus
    from iic_booking.remote_analysis.models import WorkstationStateHistory
    from iic_booking.remote_analysis.scheduler_models import MaintenanceWindow
    from iic_booking.remote_analysis.services.audit import record_event

    now = timezone.now()
    applied = 0
    for window in MaintenanceWindow.objects.filter(active=True, start__lte=now, end__gte=now).select_related(
        "workstation"
    ):
        if window.workstation_id:
            ws = window.workstation
            if ws.status != WorkstationStatus.MAINTENANCE:
                WorkstationStateHistory.objects.create(
                    workstation=ws,
                    from_status=ws.status,
                    to_status=WorkstationStatus.MAINTENANCE,
                    reason=window.reason or "Maintenance window",
                )
                ws.status = WorkstationStatus.MAINTENANCE
                ws.save(update_fields=["status", "updated_at"])
                applied += 1
                record_event(
                    category=AuditCategory.MAINTENANCE,
                    action="Applied",
                    details=window.reason,
                    workstation=ws,
                )
    return {"applied": applied}


@ra_periodic_task(name="remote_analysis.detect_reservation_conflicts")
def detect_reservation_conflicts() -> int:
    from iic_booking.remote_analysis.services.conflicts import ConflictResolver

    count = ConflictResolver().detect_all_active()
    logger.info("detect_reservation_conflicts: %s", count)
    return count


@ra_periodic_task(name="remote_analysis.refresh_availability_snapshot")
def refresh_availability_snapshot() -> dict:
    """Lightweight availability refresh — expire + health + utilization metrics."""
    from iic_booking.remote_analysis.services.scheduler import SchedulerService

    svc = SchedulerService()
    expired = svc.expire_stale()
    health = svc.refresh_health()
    stats = svc.utilization_stats()
    return {"expired": expired, "health_refreshed": health, "utilization": stats}


@ra_periodic_task(name="remote_analysis.advance_preparing_sessions")
def advance_preparing_sessions() -> dict:
    """Advance PREPARING sessions after agent prepare ack / mock fast-path; fail on timeout."""
    from django.utils import timezone

    from iic_booking.remote_analysis.constants import SessionStatus
    from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings, RemoteDesktopSession

    settings_obj = RemoteAnalysisSettings.get_solo()
    orch = SessionOrchestrator()
    advanced = 0
    timed_out = 0
    now = timezone.now()
    qs = RemoteDesktopSession.objects.filter(status=SessionStatus.PREPARING)
    for session in qs:
        if orch.try_advance_after_prepare(session):
            advanced += 1
            continue
        age = (now - session.created_at).total_seconds()
        if age >= settings_obj.prepare_timeout_seconds:
            orch.fail_session(session, "Preparation timeout")
            timed_out += 1
    return {"advanced": advanced, "timed_out": timed_out}


@ra_periodic_task(name="remote_analysis.expire_desktop_sessions")
def expire_desktop_sessions() -> dict:
    from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService

    expired = SessionCleanupService().cleanup_expired()
    idle = SessionCleanupService().cleanup_idle()
    logger.info("expire_desktop_sessions expired=%s idle=%s", expired, idle)
    return {"expired": expired, "idle_terminated": idle}


@ra_periodic_task(name="remote_analysis.monitor_session_health")
def monitor_session_health() -> int:
    from iic_booking.remote_analysis.constants import SessionStatus
    from iic_booking.remote_analysis.guacamole.health import refresh_session_health
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    qs = RemoteDesktopSession.objects.filter(
        status__in=[
            SessionStatus.PREPARING,
            SessionStatus.READY,
            SessionStatus.TOKEN_GENERATED,
            SessionStatus.LAUNCHED,
            SessionStatus.CONNECTING,
            SessionStatus.CONNECTED,
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
        ]
    )
    count = 0
    for session in qs:
        refresh_session_health(session)
        count += 1
    return count


@ra_periodic_task(name="remote_analysis.purge_expired_workspaces")
def purge_expired_workspaces() -> dict:
    """Delete storage for workspaces past retention_until (archived only)."""
    from django.utils import timezone

    from iic_booking.remote_analysis.constants import WorkspaceStatus
    from iic_booking.remote_analysis.workspace.storage import StorageManager
    from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

    now = timezone.now()
    deleted = 0
    mgr = StorageManager()
    for ws in AnalysisWorkspace.objects.filter(
        status=WorkspaceStatus.ARCHIVED,
        retention_until__lt=now,
    ):
        try:
            mgr.delete_storage(ws)
            deleted += 1
        except Exception:
            logger.exception("Failed to purge workspace %s", ws.id)
    return {"deleted": deleted}


@ra_periodic_task(name="remote_analysis.aggregate_hourly_kpis")
def aggregate_hourly_kpis() -> dict:
    from iic_booking.remote_analysis.constants import AggregationPeriod
    from iic_booking.remote_analysis.operations.analytics import AnalyticsEngine
    from iic_booking.remote_analysis.operations.performance import PerformanceMonitor
    from iic_booking.remote_analysis.operations.capacity import CapacityPlanner

    kpi = AnalyticsEngine().compute_kpis(AggregationPeriod.HOURLY)
    PerformanceMonitor().aggregate(AggregationPeriod.HOURLY)
    CapacityPlanner().snapshot(AggregationPeriod.HOURLY)
    return {"kpi_id": str(kpi.id), "period_start": kpi.period_start.isoformat()}


@ra_periodic_task(name="remote_analysis.aggregate_daily_utilization")
def aggregate_daily_utilization() -> dict:
    from iic_booking.remote_analysis.constants import AggregationPeriod
    from iic_booking.remote_analysis.operations.utilization import UtilizationEngine
    from iic_booking.remote_analysis.operations.capacity import AvailabilityEngine
    from iic_booking.remote_analysis.operations.analytics import AnalyticsEngine
    from iic_booking.remote_analysis.operations.reporting import ReportingEngine

    rows = UtilizationEngine().aggregate(AggregationPeriod.DAILY)
    AvailabilityEngine().aggregate(AggregationPeriod.DAILY)
    AnalyticsEngine().compute_session_analytics(AggregationPeriod.DAILY)
    trends = ReportingEngine().refresh_trends(AggregationPeriod.DAILY)
    return {"utilization_rows": len(rows), "trends": trends}


@ra_periodic_task(name="remote_analysis.evaluate_alerts")
def evaluate_alerts() -> dict:
    from iic_booking.remote_analysis.operations.alerts import AlertEngine

    return AlertEngine().evaluate()


@ra_periodic_task(name="remote_analysis.refresh_operations_dashboard")
def refresh_operations_dashboard() -> dict:
    from iic_booking.remote_analysis.operations.dashboards import OperationsDashboardService

    payload = OperationsDashboardService().refresh_cache()
    return {"generated_at": payload.get("generated_at")}


@ra_periodic_task(name="remote_analysis.generate_weekly_reports")
def generate_weekly_reports() -> dict:
    from iic_booking.remote_analysis.constants import ReportFormat, ReportType
    from iic_booking.remote_analysis.operations.reporting import ReportingEngine

    engine = ReportingEngine()
    r1 = engine.generate(ReportType.WEEKLY_UTILIZATION, fmt=ReportFormat.JSON)
    r2 = engine.generate(ReportType.SESSION_SUMMARY, fmt=ReportFormat.CSV)
    return {"reports": [str(r1.id), str(r2.id)]}


@ra_periodic_task(name="remote_analysis.generate_monthly_reports")
def generate_monthly_reports() -> dict:
    from iic_booking.remote_analysis.constants import ReportFormat, ReportType
    from iic_booking.remote_analysis.operations.reporting import ReportingEngine

    engine = ReportingEngine()
    r1 = engine.generate(ReportType.MONTHLY_UTILIZATION, fmt=ReportFormat.EXCEL)
    r2 = engine.generate(ReportType.CAPACITY_REPORT, fmt=ReportFormat.PDF)
    return {"reports": [str(r1.id), str(r2.id)]}


@ra_periodic_task(name="remote_analysis.archive_old_metrics")
def archive_old_metrics(days: int = 90) -> dict:
    """Delete old fine-grained performance metrics (keep aggregates)."""
    from datetime import timedelta

    from django.utils import timezone

    from iic_booking.remote_analysis.operations_models import PerformanceMetric

    cutoff = timezone.now() - timedelta(days=days)
    deleted, _ = PerformanceMetric.objects.filter(recorded_at__lt=cutoff).delete()
    return {"deleted": deleted}


@ra_periodic_task(name="remote_analysis.expire_invitations")
def expire_invitations() -> dict:
    from iic_booking.remote_analysis.sharing import InvitationService

    count = InvitationService().expire_stale()
    return {"expired": count}


@ra_periodic_task(name="remote_analysis.send_reservation_reminders")
def send_reservation_reminders() -> dict:
    """Notify users whose reservations start within their preferred reminder window."""
    from datetime import timedelta

    from django.utils import timezone

    from iic_booking.remote_analysis.collaboration_models import NotificationPreference
    from iic_booking.remote_analysis.constants import NotificationType, ReservationStatus
    from iic_booking.remote_analysis.notifications import NotificationEngine
    from iic_booking.remote_analysis.scheduler_models import AnalysisReservation

    now = timezone.now()
    sent = 0
    qs = AnalysisReservation.objects.filter(
        status__in=[ReservationStatus.RESERVED, ReservationStatus.READY, ReservationStatus.QUEUED],
        requested_start__gt=now,
        requested_start__lte=now + timedelta(hours=24),
    ).select_related("user")[:200]
    engine = NotificationEngine()
    for reservation in qs:
        prefs, _ = NotificationPreference.objects.get_or_create(user=reservation.user)
        minutes = prefs.reminder_minutes_before or 30
        window_start = reservation.requested_start - timedelta(minutes=minutes)
        window_end = window_start + timedelta(minutes=5)
        if not (window_start <= now <= window_end):
            continue
        # Avoid duplicate reminders in the same hour via metadata check
        from iic_booking.remote_analysis.collaboration_models import Notification

        already = Notification.objects.filter(
            user=reservation.user,
            notification_type=NotificationType.RESERVATION_REMINDER,
            metadata__reservation_id=str(reservation.id),
            created_at__gte=now - timedelta(hours=2),
        ).exists()
        if already:
            continue
        engine.notify(
            reservation.user,
            NotificationType.RESERVATION_REMINDER,
            "Reservation reminder",
            f"Your analysis reservation starts at {reservation.requested_start.isoformat()}",
            metadata={"reservation_id": str(reservation.id)},
        )
        sent += 1
    return {"sent": sent}
