"""Alert Engine — evaluate rules and manage alert lifecycle."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    HEARTBEAT_OFFLINE_SECONDS,
    HIGH_CPU_THRESHOLD,
    LOW_MEMORY_THRESHOLD,
    DISK_FULL_THRESHOLD,
    AlertCategory,
    AlertSeverity,
    AlertStatus,
    AuditCategory,
    SessionStatus,
    TransferStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationHeartbeat
from iic_booking.remote_analysis.operations_models import AlertEvent, AlertRule
from iic_booking.remote_analysis.scheduler_models import ReservationConflict
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceTransfer


DEFAULT_RULES = [
    ("Agent Offline", AlertCategory.AGENT, AlertSeverity.CRITICAL, "agent_offline", "eq", 1, 2),
    ("Heartbeat Timeout", AlertCategory.HEARTBEAT, AlertSeverity.WARNING, "heartbeat_age_seconds", "gt", HEARTBEAT_OFFLINE_SECONDS, 2),
    ("High CPU", AlertCategory.PERFORMANCE, AlertSeverity.WARNING, "cpu", "gt", HIGH_CPU_THRESHOLD, 5),
    ("Low Memory", AlertCategory.PERFORMANCE, AlertSeverity.WARNING, "memory", "gt", LOW_MEMORY_THRESHOLD, 5),
    ("Low Disk", AlertCategory.PERFORMANCE, AlertSeverity.CRITICAL, "disk", "gt", DISK_FULL_THRESHOLD, 5),
    ("Repeated Session Failures", AlertCategory.SESSION, AlertSeverity.WARNING, "session_failures", "gte", 3, 60),
    ("Repeated Sync Failures", AlertCategory.SYNC, AlertSeverity.WARNING, "sync_failures", "gte", 3, 60),
    ("Reservation Conflicts", AlertCategory.RESERVATION, AlertSeverity.WARNING, "open_conflicts", "gte", 1, 15),
    ("Workspace Quota Exceeded", AlertCategory.WORKSPACE, AlertSeverity.WARNING, "quota_pct", "gte", 95, 15),
    ("Excessive Idle Sessions", AlertCategory.SESSION, AlertSeverity.INFO, "idle_sessions", "gte", 3, 15),
]


class AlertEngine:
    def ensure_default_rules(self) -> int:
        created = 0
        for name, cat, sev, metric, op, thr, window in DEFAULT_RULES:
            _, was_created = AlertRule.objects.get_or_create(
                name=name,
                defaults={
                    "category": cat,
                    "severity": sev,
                    "metric_name": metric,
                    "operator": op,
                    "threshold": thr,
                    "window_minutes": window,
                    "is_active": True,
                    "description": f"Auto rule: {name}",
                },
            )
            if was_created:
                created += 1
        return created

    def _compare(self, value: float, operator: str, threshold: float) -> bool:
        if operator == "gt":
            return value > threshold
        if operator == "gte":
            return value >= threshold
        if operator == "lt":
            return value < threshold
        if operator == "lte":
            return value <= threshold
        if operator == "eq":
            return value == threshold
        return False

    def _emit(self, rule: AlertRule, title: str, message: str, *, workstation=None, metadata=None) -> AlertEvent | None:
        # Deduplicate open alerts with same title+workstation in last window
        since = timezone.now() - timedelta(minutes=rule.window_minutes or 5)
        exists = AlertEvent.objects.filter(
            title=title,
            workstation=workstation,
            status__in=[AlertStatus.OPEN, AlertStatus.ACKNOWLEDGED],
            created_at__gte=since,
        ).exists()
        if exists:
            return None
        event = AlertEvent.objects.create(
            rule=rule,
            severity=rule.severity,
            category=rule.category,
            status=AlertStatus.OPEN,
            title=title,
            message=message,
            workstation=workstation,
            metadata=metadata or {},
        )
        record_event(
            category=AuditCategory.ALERTS,
            action="AlertRaised",
            details=title,
            workstation=workstation,
            success=True,
            correlation_id=str(event.id),
        )
        try:
            from iic_booking.remote_analysis.collaboration.hooks import on_alert_raised

            on_alert_raised(event)
        except Exception:
            pass
        return event

    def evaluate(self) -> dict:
        self.ensure_default_rules()
        raised = 0
        now = timezone.now()

        for rule in AlertRule.objects.filter(is_active=True):
            metric = rule.metric_name
            window_start = now - timedelta(minutes=rule.window_minutes or 5)

            if metric in {"agent_offline", "heartbeat_age_seconds", "cpu", "memory", "disk"}:
                for ws in AnalysisWorkstation.objects.filter(enabled=True):
                    last = WorkstationHeartbeat.objects.filter(workstation=ws).order_by("-received_at").first()
                    value = 0.0
                    if metric == "agent_offline":
                        offline = (
                            ws.status == WorkstationStatus.OFFLINE
                            or not ws.last_heartbeat
                            or (now - ws.last_heartbeat).total_seconds() > HEARTBEAT_OFFLINE_SECONDS
                        )
                        value = 1.0 if offline else 0.0
                    elif metric == "heartbeat_age_seconds":
                        value = (now - ws.last_heartbeat).total_seconds() if ws.last_heartbeat else 99999
                    elif last:
                        value = float(getattr(last, metric, 0) or 0)
                    if self._compare(value, rule.operator, rule.threshold):
                        if self._emit(rule, rule.name, f"{ws.hostname}: {metric}={value}", workstation=ws, metadata={"value": value}):
                            raised += 1

            elif metric == "session_failures":
                count = RemoteDesktopSession.objects.filter(
                    status=SessionStatus.FAILED, created_at__gte=window_start
                ).count()
                if self._compare(count, rule.operator, rule.threshold):
                    if self._emit(rule, rule.name, f"{count} session failures in window", metadata={"count": count}):
                        raised += 1

            elif metric == "sync_failures":
                count = WorkspaceTransfer.objects.filter(
                    status=TransferStatus.FAILED, created_at__gte=window_start
                ).count()
                if self._compare(count, rule.operator, rule.threshold):
                    if self._emit(rule, rule.name, f"{count} sync/transfer failures", metadata={"count": count}):
                        raised += 1

            elif metric == "open_conflicts":
                count = ReservationConflict.objects.filter(resolved=False).count()
                if self._compare(count, rule.operator, rule.threshold):
                    if self._emit(rule, rule.name, f"{count} unresolved conflicts", metadata={"count": count}):
                        raised += 1

            elif metric == "quota_pct":
                for ws_obj in AnalysisWorkspace.objects.filter(quota_gb__gt=0):
                    hard = ws_obj.quota_gb * (1024**3)
                    pct = 100.0 * ws_obj.current_usage_bytes / hard if hard else 0
                    if self._compare(pct, rule.operator, rule.threshold):
                        if self._emit(
                            rule,
                            rule.name,
                            f"Workspace {ws_obj.id} at {pct:.1f}%",
                            workstation=ws_obj.workstation,
                            metadata={"quota_pct": pct},
                        ):
                            raised += 1

            elif metric == "idle_sessions":
                count = RemoteDesktopSession.objects.filter(status=SessionStatus.IDLE).count()
                if self._compare(count, rule.operator, rule.threshold):
                    if self._emit(rule, rule.name, f"{count} idle sessions", metadata={"count": count}):
                        raised += 1

        return {"raised": raised, "evaluated_at": now.isoformat()}

    def acknowledge(self, alert: AlertEvent, user) -> AlertEvent:
        alert.acknowledged = True
        alert.acknowledged_by = user
        alert.acknowledged_at = timezone.now()
        alert.status = AlertStatus.ACKNOWLEDGED
        alert.save(update_fields=["acknowledged", "acknowledged_by", "acknowledged_at", "status"])
        record_event(
            category=AuditCategory.ALERTS,
            action="AlertAcknowledged",
            details=alert.title,
            actor=user,
            correlation_id=str(alert.id),
        )
        return alert

    def resolve(self, alert: AlertEvent, user) -> AlertEvent:
        alert.resolved = True
        alert.resolved_by = user
        alert.resolved_at = timezone.now()
        alert.status = AlertStatus.RESOLVED
        alert.save(update_fields=["resolved", "resolved_by", "resolved_at", "status"])
        record_event(
            category=AuditCategory.ALERTS,
            action="AlertResolved",
            details=alert.title,
            actor=user,
            correlation_id=str(alert.id),
        )
        return alert

    def list_alerts(self, *, status: str | None = None, limit: int = 100) -> list[AlertEvent]:
        qs = AlertEvent.objects.select_related("workstation", "rule", "assigned_to").order_by("-created_at")
        if status:
            qs = qs.filter(status=status.upper())
        return list(qs[:limit])
