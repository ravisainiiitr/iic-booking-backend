"""Operations dashboards — compose executive / ops / performance views."""

from __future__ import annotations

from django.utils import timezone

from iic_booking.remote_analysis.constants import AggregationPeriod, AuditCategory, SessionStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.operations.analytics import AnalyticsEngine
from iic_booking.remote_analysis.operations.alerts import AlertEngine
from iic_booking.remote_analysis.operations.capacity import AvailabilityEngine, CapacityPlanner
from iic_booking.remote_analysis.operations.performance import PerformanceMonitor
from iic_booking.remote_analysis.operations.reporting import ReportingEngine
from iic_booking.remote_analysis.operations.utilization import UtilizationEngine
from iic_booking.remote_analysis.operations_models import DashboardSnapshot, UsageTrend
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationQueue
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace


class OperationsDashboardService:
    def build(self, *, refresh: bool = False) -> dict:
        if not refresh:
            snap = DashboardSnapshot.objects.filter(dashboard_key="operations").order_by("-generated_at").first()
            if snap and (timezone.now() - snap.generated_at).total_seconds() < 60:
                return snap.payload

        analytics = AnalyticsEngine()
        kpis = analytics.compute_kpis(AggregationPeriod.HOURLY)
        payload = {
            "executive": {
                "total_workstations": kpis.total_workstations,
                "online_workstations": kpis.online_workstations,
                "busy_workstations": kpis.busy_workstations,
                "available_workstations": kpis.available_workstations,
                "average_utilization": kpis.average_utilization,
                "availability_percent": kpis.availability_percent,
                "session_success_rate": kpis.session_success_rate,
                "reservation_success_rate": kpis.reservation_success_rate,
                "open_alerts": kpis.open_alerts,
                "current_queue_length": kpis.current_queue_length,
            },
            "operations": {
                "live_workstations": list(
                    AnalysisWorkstation.objects.values(
                        "id", "hostname", "status", "health_score", "last_heartbeat"
                    )[:100]
                ),
                "current_sessions": list(
                    RemoteDesktopSession.objects.filter(
                        status__in=[
                            SessionStatus.ACTIVE,
                            SessionStatus.CONNECTED,
                            SessionStatus.IDLE,
                            SessionStatus.LAUNCHED,
                            SessionStatus.PREPARING,
                        ]
                    ).values("id", "status", "user__email", "workstation__hostname", "created_at")[:50]
                ),
                "current_reservations": list(
                    AnalysisReservation.objects.exclude(status__in=["COMPLETED", "CANCELLED", "EXPIRED", "FAILED"])
                    .values("id", "status", "user__email", "workstation__hostname", "requested_start", "requested_end")[:50]
                ),
            },
            "performance": PerformanceMonitor().summary(AggregationPeriod.HOURLY),
            "utilization": UtilizationEngine().summary(AggregationPeriod.DAILY),
            "capacity": CapacityPlanner().summary(AggregationPeriod.HOURLY),
            "availability": AvailabilityEngine().summary(AggregationPeriod.DAILY),
            "alerts": [
                {
                    "id": str(a.id),
                    "title": a.title,
                    "severity": a.severity,
                    "category": a.category,
                    "status": a.status,
                    "workstation": getattr(a.workstation, "hostname", None),
                    "created_at": a.created_at.isoformat(),
                }
                for a in AlertEngine().list_alerts(limit=30)
            ],
            "analytics": analytics.analytics_payload(AggregationPeriod.DAILY),
            "trends": list(
                UsageTrend.objects.order_by("-period_start")[:50].values(
                    "metric_name", "period", "period_start", "value", "unit"
                )
            ),
            "workspaces": {
                "total": AnalysisWorkspace.objects.count(),
                "active": AnalysisWorkspace.objects.filter(status__in=["READY", "ACTIVE", "SYNCING"]).count(),
            },
            "generated_at": timezone.now().isoformat(),
        }
        DashboardSnapshot.objects.create(dashboard_key="operations", payload=payload)
        return payload

    def refresh_cache(self, *, actor=None) -> dict:
        payload = self.build(refresh=True)
        record_event(
            category=AuditCategory.OPERATIONS,
            action="DashboardCacheRefresh",
            details="operations",
            actor=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
        )
        return payload
