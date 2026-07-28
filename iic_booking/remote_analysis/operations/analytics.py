"""Analytics Engine — session analytics and KPI computation."""

from __future__ import annotations

from django.db.models import Avg, Count, Max, Min, Q, Sum
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AggregationPeriod,
    QueueEntryStatus,
    ReservationStatus,
    SessionStatus,
    TransferStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.operations.utilization import _period_bounds
from iic_booking.remote_analysis.operations_models import AlertEvent, OperationalKPI, SessionAnalytics
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationQueue
from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionStatistics
from iic_booking.remote_analysis.workspace_models import WorkspaceTelemetry, WorkspaceTransfer


class AnalyticsEngine:
    def compute_session_analytics(self, period: str = AggregationPeriod.DAILY, *, now=None) -> SessionAnalytics:
        start, end = _period_bounds(period, now)
        sessions = RemoteDesktopSession.objects.filter(created_at__gte=start, created_at__lt=end)
        stats = SessionStatistics.objects.filter(session__in=sessions)
        total = sessions.count()
        durations = list(stats.values_list("duration_seconds", flat=True))
        avg_dur = (stats.aggregate(a=Avg("duration_seconds"))["a"] or 0)
        longest = max(durations) if durations else 0
        shortest = min(durations) if durations else 0
        idle = stats.aggregate(a=Avg("idle_seconds"))["a"] or 0
        idle_pct = (100.0 * idle / avg_dur) if avg_dur else 0
        prep = stats.aggregate(a=Avg("prepare_latency_ms"))["a"] or 0
        launch = stats.aggregate(a=Avg("launch_latency_ms"))["a"] or 0
        reconnects = stats.aggregate(s=Sum("reconnect_count"))["s"] or 0

        sync_ms = (
            WorkspaceTelemetry.objects.filter(
                metric_name__icontains="sync",
                recorded_at__gte=start,
                recorded_at__lt=end,
            ).aggregate(a=Avg("value"))["a"]
            or 0
        )
        cleanup_ms = (
            WorkspaceTelemetry.objects.filter(
                metric_name="archive_time_ms",
                recorded_at__gte=start,
                recorded_at__lt=end,
            ).aggregate(a=Avg("value"))["a"]
            or 0
        )

        cancelled = sessions.filter(status__in=[SessionStatus.TERMINATED, SessionStatus.FAILED]).count()
        completed = sessions.filter(status__in=[SessionStatus.COMPLETED, SessionStatus.TERMINATED]).count()
        no_show = sessions.filter(
            status__in=[SessionStatus.EXPIRED, SessionStatus.FAILED],
            connected_at__isnull=True,
        ).count()
        success = sessions.filter(
            status__in=[SessionStatus.COMPLETED, SessionStatus.ACTIVE, SessionStatus.CONNECTED]
        ).count()

        row, _ = SessionAnalytics.objects.update_or_create(
            period=period,
            period_start=start,
            defaults={
                "period_end": end,
                "total_sessions": total,
                "average_duration_seconds": round(avg_dur, 2),
                "longest_session_seconds": round(longest, 2),
                "shortest_session_seconds": round(shortest, 2),
                "idle_percentage": round(idle_pct, 2),
                "average_preparation_ms": round(prep, 2),
                "average_cleanup_ms": round(cleanup_ms, 2),
                "average_sync_ms": round(sync_ms, 2),
                "average_launch_ms": round(launch, 2),
                "average_disconnect_seconds": 0,
                "reconnect_count": reconnects,
                "cancellation_rate": round(cancelled / total, 4) if total else 0,
                "no_show_rate": round(no_show / total, 4) if total else 0,
                "success_rate": round(success / total, 4) if total else 0,
            },
        )
        return row

    def compute_kpis(self, period: str = AggregationPeriod.HOURLY, *, now=None) -> OperationalKPI:
        start, end = _period_bounds(period, now)
        ws = AnalysisWorkstation.objects.all()
        total = ws.count()
        online = ws.filter(
            status__in=[
                WorkstationStatus.ONLINE,
                WorkstationStatus.AVAILABLE,
                WorkstationStatus.BUSY,
                WorkstationStatus.PREPARING,
            ]
        ).count()
        busy = ws.filter(status=WorkstationStatus.BUSY).count()
        available = ws.filter(status=WorkstationStatus.AVAILABLE).count()

        session_analytics = self.compute_session_analytics(period, now=now)
        from iic_booking.remote_analysis.operations.utilization import UtilizationEngine

        util = UtilizationEngine().summary(period if period != AggregationPeriod.HOURLY else AggregationPeriod.DAILY)

        reservations = AnalysisReservation.objects.filter(created_at__gte=start, created_at__lt=end)
        res_total = reservations.count() or 1
        res_ok = reservations.filter(
            status__in=[ReservationStatus.RESERVED, ReservationStatus.READY, ReservationStatus.ACTIVE, ReservationStatus.COMPLETED]
        ).count()

        transfers = WorkspaceTransfer.objects.filter(created_at__gte=start, created_at__lt=end)
        t_total = transfers.count() or 1
        t_ok = transfers.filter(status=TransferStatus.COMPLETED).count()

        queue_len = ReservationQueue.objects.filter(status=QueueEntryStatus.WAITING).count()
        open_alerts = AlertEvent.objects.filter(status__in=["OPEN", "ACKNOWLEDGED"]).count()

        row, _ = OperationalKPI.objects.update_or_create(
            period=period,
            period_start=start,
            defaults={
                "total_workstations": total,
                "online_workstations": online,
                "busy_workstations": busy,
                "available_workstations": available,
                "average_utilization": util.get("average_utilization") or 0,
                "average_session_duration": session_analytics.average_duration_seconds,
                "session_success_rate": session_analytics.success_rate,
                "reservation_success_rate": round(res_ok / res_total, 4),
                "workspace_transfer_success": round(t_ok / t_total, 4),
                "average_preparation_ms": session_analytics.average_preparation_ms,
                "average_cleanup_ms": session_analytics.average_cleanup_ms,
                "average_sync_ms": session_analytics.average_sync_ms,
                "average_launch_ms": session_analytics.average_launch_ms,
                "availability_percent": util.get("average_availability") or 100,
                "current_queue_length": queue_len,
                "open_alerts": open_alerts,
            },
        )
        return row

    def analytics_payload(self, period: str = AggregationPeriod.DAILY) -> dict:
        row = SessionAnalytics.objects.filter(period=period).order_by("-period_start").first()
        if not row:
            row = self.compute_session_analytics(period)
        kpi = OperationalKPI.objects.filter(period=AggregationPeriod.HOURLY).order_by("-period_start").first()
        if not kpi:
            kpi = self.compute_kpis(AggregationPeriod.HOURLY)
        return {
            "session_analytics": {
                "period": row.period,
                "period_start": row.period_start.isoformat(),
                "total_sessions": row.total_sessions,
                "average_duration_seconds": row.average_duration_seconds,
                "longest_session_seconds": row.longest_session_seconds,
                "shortest_session_seconds": row.shortest_session_seconds,
                "idle_percentage": row.idle_percentage,
                "average_preparation_ms": row.average_preparation_ms,
                "average_cleanup_ms": row.average_cleanup_ms,
                "average_sync_ms": row.average_sync_ms,
                "average_launch_ms": row.average_launch_ms,
                "reconnect_count": row.reconnect_count,
                "cancellation_rate": row.cancellation_rate,
                "no_show_rate": row.no_show_rate,
                "success_rate": row.success_rate,
            },
            "kpis": {
                "total_workstations": kpi.total_workstations,
                "online_workstations": kpi.online_workstations,
                "busy_workstations": kpi.busy_workstations,
                "available_workstations": kpi.available_workstations,
                "average_utilization": kpi.average_utilization,
                "average_session_duration": kpi.average_session_duration,
                "session_success_rate": kpi.session_success_rate,
                "reservation_success_rate": kpi.reservation_success_rate,
                "workspace_transfer_success": kpi.workspace_transfer_success,
                "average_preparation_ms": kpi.average_preparation_ms,
                "average_cleanup_ms": kpi.average_cleanup_ms,
                "average_sync_ms": kpi.average_sync_ms,
                "average_launch_ms": kpi.average_launch_ms,
                "availability_percent": kpi.availability_percent,
                "current_queue_length": kpi.current_queue_length,
                "open_alerts": kpi.open_alerts,
            },
        }
