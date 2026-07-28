"""GuacamoleIntegrationService — facade used by APIs and Celery jobs."""

from __future__ import annotations

from typing import Any

from django.db.models import Avg, Sum
from django.utils import timezone

from iic_booking.remote_analysis.constants import SessionStatus
from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.guacamole.connection import ConnectionManager
from iic_booking.remote_analysis.guacamole.health import refresh_session_health
from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import (
    ConnectionHistory,
    RemoteAnalysisSettings,
    RemoteDesktopSession,
    SessionStatistics,
)


class GuacamoleIntegrationService:
    """
    Authenticate with Guacamole, create temporary connections/users,
    generate session tokens, launch browser sessions, terminate, and cleanup.
    """

    def __init__(self):
        self.settings = RemoteAnalysisSettings.get_solo()
        self.orchestrator = SessionOrchestrator()
        self.connections = ConnectionManager(self.settings)
        self.cleanup = SessionCleanupService()
        self.client = GuacamoleClient(self.settings)

    def authenticate(self) -> str:
        return self.client.authenticate()

    def create_session_for_reservation(self, reservation_id, user, **kwargs) -> RemoteDesktopSession:
        reservation = AnalysisReservation.objects.select_related("workstation", "user", "booking").get(pk=reservation_id)
        return self.orchestrator.create_session(reservation=reservation, user=user, **kwargs)

    def launch(self, session: RemoteDesktopSession, user, **kwargs) -> dict[str, Any]:
        return self.orchestrator.build_launch_payload(session, user=user, **kwargs)

    def connect(self, session: RemoteDesktopSession, token: str, user, **kwargs) -> dict[str, Any]:
        return self.orchestrator.connect_with_token(session, token, user=user, **kwargs)

    def terminate(self, session: RemoteDesktopSession, user=None, reason: str = "Terminated") -> RemoteDesktopSession:
        return self.orchestrator.terminate(session, user=user, reason=reason)

    def retry_prepare(self, session: RemoteDesktopSession) -> bool:
        return self.orchestrator.try_advance_after_prepare(session)

    def health(self, session: RemoteDesktopSession):
        return refresh_session_health(session)

    def guacamole_reachable(self) -> bool:
        return self.client.health_check()

    def dashboard_metrics(self) -> dict[str, Any]:
        now = timezone.now()
        open_qs = RemoteDesktopSession.objects.filter(
            status__in=[
                SessionStatus.CREATED,
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
        stats = SessionStatistics.objects.all()
        agg = stats.aggregate(
            avg_duration=Avg("duration_seconds"),
            avg_idle=Avg("idle_seconds"),
            avg_launch=Avg("launch_latency_ms"),
            total_bytes_in=Sum("bytes_in"),
            total_bytes_out=Sum("bytes_out"),
        )
        total = RemoteDesktopSession.objects.count()
        failed = RemoteDesktopSession.objects.filter(status=SessionStatus.FAILED).count()
        recent_disconnects = list(
            ConnectionHistory.objects.filter(event__in=["connected", "state"])
            .filter(detail__icontains="DISCONNECT")
            .order_by("-created_at")[:10]
            .values("session_id", "event", "detail", "created_at")
        )
        # Also include recently terminated sessions
        recent_term = list(
            RemoteDesktopSession.objects.filter(
                status__in=[SessionStatus.TERMINATED, SessionStatus.COMPLETED, SessionStatus.EXPIRED]
            )
            .order_by("-disconnected_at", "-updated_at")[:10]
            .values("id", "status", "termination_reason", "disconnected_at", "workstation__hostname", "user__email")
        )
        return {
            "active_sessions": open_qs.filter(status__in=[SessionStatus.ACTIVE, SessionStatus.CONNECTED]).count(),
            "idle_sessions": open_qs.filter(status=SessionStatus.IDLE).count(),
            "preparing_sessions": open_qs.filter(status=SessionStatus.PREPARING).count(),
            "browser_sessions": open_qs.filter(
                status__in=[SessionStatus.LAUNCHED, SessionStatus.CONNECTING, SessionStatus.CONNECTED, SessionStatus.ACTIVE]
            ).count(),
            "open_sessions": open_qs.count(),
            "total_sessions": total,
            "failure_rate": (failed / total) if total else 0.0,
            "average_duration_seconds": agg["avg_duration"] or 0,
            "average_idle_seconds": agg["avg_idle"] or 0,
            "average_launch_latency_ms": agg["avg_launch"] or 0,
            "bandwidth_bytes_in": agg["total_bytes_in"] or 0,
            "bandwidth_bytes_out": agg["total_bytes_out"] or 0,
            "guacamole_reachable": self.guacamole_reachable(),
            "mock_guacamole": bool(self.settings.mock_guacamole),
            "recent_disconnects": recent_term,
            "connection_events": recent_disconnects,
            "timeline": list(
                open_qs.order_by("-created_at")[:25].values(
                    "id",
                    "status",
                    "created_at",
                    "launch_time",
                    "connected_at",
                    "workstation__hostname",
                    "user__email",
                )
            ),
            "checked_at": now.isoformat(),
        }
