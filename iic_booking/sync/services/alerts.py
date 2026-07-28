"""Intelligent alert engine and lifecycle (Milestone 15)."""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from iic_booking.sync.models import (
    AlertEvent,
    AlertLifecycleStatus,
    AlertSeverity,
    DepartmentSyncAgent,
    SyncLogCategory,
    SyncLogSeverity,
)
from iic_booking.sync.services.logging import write_sync_log

logger = logging.getLogger(__name__)

# Default thresholds (portal-side evaluation of agent health snapshots).
DEFAULT_RULES: list[dict[str, Any]] = [
    {"code": "AGENT_OFFLINE", "category": "availability", "severity": AlertSeverity.CRITICAL, "title": "Agent offline"},
    {"code": "HIGH_CPU", "category": "resource", "severity": AlertSeverity.WARNING, "title": "High CPU", "threshold": 90.0},
    {"code": "HIGH_MEMORY", "category": "resource", "severity": AlertSeverity.WARNING, "title": "High memory", "threshold": 90.0},
    {"code": "LOW_DISK", "category": "resource", "severity": AlertSeverity.ERROR, "title": "Low disk", "threshold": 10.0},
    {"code": "QUEUE_GROWTH", "category": "queue", "severity": AlertSeverity.WARNING, "title": "Queue growth", "threshold": 500},
    {"code": "UPLOAD_FAILURES", "category": "upload", "severity": AlertSeverity.ERROR, "title": "Upload failures"},
    {"code": "PROCESSING_FAILURES", "category": "processing", "severity": AlertSeverity.ERROR, "title": "Processing failures"},
    {"code": "SECURITY_FAILURE", "category": "security", "severity": AlertSeverity.CRITICAL, "title": "Security failure"},
    {"code": "RECOVERY_FAILURE", "category": "recovery", "severity": AlertSeverity.ERROR, "title": "Recovery failure"},
    {"code": "PLUGIN_CRASH", "category": "plugin", "severity": AlertSeverity.ERROR, "title": "Plugin crash"},
    {"code": "HEARTBEAT_MISSING", "category": "availability", "severity": AlertSeverity.CRITICAL, "title": "Heartbeat missing"},
    {"code": "PORTAL_LATENCY", "category": "network", "severity": AlertSeverity.WARNING, "title": "Portal latency", "threshold": 5000.0},
    {"code": "REPEATED_RETRY", "category": "upload", "severity": AlertSeverity.WARNING, "title": "Repeated retry", "threshold": 10},
]


def _fingerprint(*parts: Any) -> str:
    raw = "|".join(str(p or "") for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:64]


class AlertService:
    def scoped_queryset(self, *, department_id=None, status: str | None = None) -> QuerySet[AlertEvent]:
        qs = AlertEvent.objects.select_related("sync_agent", "department", "building", "equipment")
        if department_id:
            qs = qs.filter(Q(department_id=department_id) | Q(sync_agent__department_id=department_id))
        if status:
            qs = qs.filter(status=status)
        return qs

    def list_alerts(
        self,
        *,
        department_id=None,
        status: str | None = None,
        severity: str | None = None,
        agent_id=None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        qs = self.scoped_queryset(department_id=department_id, status=status)
        if severity:
            qs = qs.filter(severity=severity)
        if agent_id:
            qs = qs.filter(sync_agent_id=agent_id)
        rows = []
        for alert in qs[: max(1, min(limit, 500))]:
            rows.append(self._serialize(alert))
        return rows

    def summary(self, *, department_id=None) -> dict[str, Any]:
        qs = self.scoped_queryset(department_id=department_id).exclude(
            status__in=[AlertLifecycleStatus.RESOLVED, AlertLifecycleStatus.EXPIRED]
        )
        by_severity = {
            row["severity"]: row["c"]
            for row in qs.values("severity").annotate(c=Count("id"))
        }
        by_status = {
            row["status"]: row["c"]
            for row in qs.values("status").annotate(c=Count("id"))
        }
        return {
            "open_total": qs.count(),
            "by_severity": by_severity,
            "by_status": by_status,
            "critical": by_severity.get(AlertSeverity.CRITICAL, 0),
            "error": by_severity.get(AlertSeverity.ERROR, 0),
            "warning": by_severity.get(AlertSeverity.WARNING, 0),
            "info": by_severity.get(AlertSeverity.INFO, 0),
        }

    def raise_alert(
        self,
        *,
        rule_code: str,
        category: str,
        severity: str,
        title: str,
        message: str,
        sync_agent: DepartmentSyncAgent | None = None,
        department=None,
        building=None,
        equipment=None,
        correlation_id=None,
        details: dict | None = None,
        expires_hours: int = 24,
    ) -> AlertEvent:
        agent = sync_agent
        dept = department or (getattr(agent, "department", None) if agent else None)
        bld = building or (getattr(agent, "building", None) if agent else None)
        fp = _fingerprint(rule_code, getattr(agent, "id", None), category, title)
        existing = (
            AlertEvent.objects.filter(
                fingerprint=fp,
                status__in=[
                    AlertLifecycleStatus.NEW,
                    AlertLifecycleStatus.ACKNOWLEDGED,
                    AlertLifecycleStatus.SUPPRESSED,
                ],
            )
            .order_by("-created_at")
            .first()
        )
        if existing:
            existing.message = message[:1000]
            existing.details = {**(existing.details or {}), **(details or {})}
            existing.updated_at = timezone.now()
            existing.save(update_fields=["message", "details", "updated_at"])
            return existing

        alert = AlertEvent.objects.create(
            correlation_id=correlation_id or uuid.uuid4(),
            department=dept,
            building=bld,
            sync_agent=agent,
            equipment=equipment,
            rule_code=rule_code[:64],
            category=category[:64],
            severity=severity,
            status=AlertLifecycleStatus.NEW,
            title=title[:200],
            message=message[:1000],
            fingerprint=fp,
            details=details or {},
            expires_at=timezone.now() + timedelta(hours=expires_hours),
        )
        write_sync_log(
            event_code="MON-ALERT",
            category=SyncLogCategory.MONITORING,
            severity=SyncLogSeverity.WARNING
            if severity in (AlertSeverity.INFO, AlertSeverity.WARNING)
            else SyncLogSeverity.ERROR,
            message=f"Alert raised: {rule_code} — {title}",
            sync_agent=agent,
            correlation_id=alert.correlation_id,
            json_payload={"alert_id": str(alert.id), "severity": severity, "category": category},
        )
        logger.info("monitoring.alert_generated rule=%s alert_id=%s", rule_code, alert.id)
        return alert

    def acknowledge(self, alert_id, *, user_name: str = "") -> dict[str, Any]:
        alert = AlertEvent.objects.filter(pk=alert_id).first()
        if alert is None:
            raise ValueError("Alert not found.")
        if alert.status in (AlertLifecycleStatus.RESOLVED, AlertLifecycleStatus.EXPIRED):
            raise ValueError(f"Cannot acknowledge alert in status {alert.status}.")
        alert.status = AlertLifecycleStatus.ACKNOWLEDGED
        alert.acknowledged_by = (user_name or "")[:200]
        alert.acknowledged_at = timezone.now()
        alert.save(update_fields=["status", "acknowledged_by", "acknowledged_at", "updated_at"])
        write_sync_log(
            event_code="MON-ACK",
            category=SyncLogCategory.MONITORING,
            severity=SyncLogSeverity.INFO,
            message=f"Alert acknowledged: {alert.rule_code}",
            sync_agent=alert.sync_agent,
            correlation_id=alert.correlation_id,
            json_payload={"alert_id": str(alert.id), "user": user_name},
        )
        return self._serialize(alert)

    def resolve(self, alert_id, *, user_name: str = "", resolution: str = "") -> dict[str, Any]:
        alert = AlertEvent.objects.filter(pk=alert_id).first()
        if alert is None:
            raise ValueError("Alert not found.")
        alert.status = AlertLifecycleStatus.RESOLVED
        alert.resolved_by = (user_name or "")[:200]
        alert.resolved_at = timezone.now()
        alert.resolution = (resolution or "")[:500]
        alert.save(
            update_fields=["status", "resolved_by", "resolved_at", "resolution", "updated_at"]
        )
        write_sync_log(
            event_code="MON-RESOLVE",
            category=SyncLogCategory.MONITORING,
            severity=SyncLogSeverity.INFO,
            message=f"Alert resolved: {alert.rule_code}",
            sync_agent=alert.sync_agent,
            correlation_id=alert.correlation_id,
            json_payload={"alert_id": str(alert.id), "resolution": resolution},
        )
        logger.info("monitoring.alert_resolution alert_id=%s", alert.id)
        return self._serialize(alert)

    def expire_stale(self) -> int:
        now = timezone.now()
        updated = AlertEvent.objects.filter(
            status__in=[AlertLifecycleStatus.NEW, AlertLifecycleStatus.ACKNOWLEDGED],
            expires_at__isnull=False,
            expires_at__lt=now,
        ).update(status=AlertLifecycleStatus.EXPIRED, updated_at=now)
        return updated

    def evaluate_health_snapshot(self, snapshot, *, metrics: dict | None = None) -> list[AlertEvent]:
        """Evaluate configurable rules against a persisted AgentHealthSnapshot."""
        agent = snapshot.sync_agent
        raised: list[AlertEvent] = []
        m = metrics or (snapshot.metrics or {})

        def _raise(code: str, message: str, **kwargs):
            rule = next((r for r in DEFAULT_RULES if r["code"] == code), None)
            if rule is None:
                return
            raised.append(
                self.raise_alert(
                    rule_code=code,
                    category=rule["category"],
                    severity=kwargs.get("severity") or rule["severity"],
                    title=rule["title"],
                    message=message,
                    sync_agent=agent,
                    department=snapshot.department,
                    building=snapshot.building,
                    correlation_id=snapshot.correlation_id,
                    details={"snapshot_id": snapshot.id, **(kwargs.get("details") or {})},
                )
            )

        free_pct = None
        if snapshot.disk_used_percent is not None:
            free_pct = max(0.0, 100.0 - float(snapshot.disk_used_percent))
        elif snapshot.disk_free_bytes is not None and m.get("disk_total_bytes"):
            total = float(m["disk_total_bytes"]) or 1.0
            free_pct = (float(snapshot.disk_free_bytes) / total) * 100.0

        if snapshot.cpu_percent is not None and snapshot.cpu_percent >= 90:
            _raise("HIGH_CPU", f"CPU at {snapshot.cpu_percent:.1f}%")
        mem_pct = snapshot.memory_percent
        if mem_pct is not None and mem_pct >= 90:
            _raise("HIGH_MEMORY", f"Memory at {mem_pct:.1f}%")
        if free_pct is not None and free_pct <= 10:
            _raise("LOW_DISK", f"Disk free {free_pct:.1f}%")
        queue_total = (
            (snapshot.upload_queue_size or 0)
            + (snapshot.processing_queue_size or 0)
            + (snapshot.discovery_queue_size or 0)
        )
        if queue_total >= 500:
            _raise("QUEUE_GROWTH", f"Combined queues at {queue_total}")
        if snapshot.portal_latency_ms is not None and snapshot.portal_latency_ms >= 5000:
            _raise("PORTAL_LATENCY", f"Portal latency {snapshot.portal_latency_ms:.0f} ms")
        if (m.get("failed_uploads") or 0) > 0:
            _raise("UPLOAD_FAILURES", f"Failed uploads: {m.get('failed_uploads')}")
        if (m.get("failed_processing") or 0) > 0:
            _raise("PROCESSING_FAILURES", f"Failed processing: {m.get('failed_processing')}")
        if (m.get("retry_count") or 0) >= 10:
            _raise("REPEATED_RETRY", f"Retry count {m.get('retry_count')}")
        sec = (snapshot.security_status or "").lower()
        if sec in ("failed", "error", "critical", "compromised"):
            _raise("SECURITY_FAILURE", f"Security status: {snapshot.security_status}")
        rec = (snapshot.recovery_state or "").lower()
        if rec in ("failed", "error", "critical"):
            _raise("RECOVERY_FAILURE", f"Recovery state: {snapshot.recovery_state}")
        plug = (snapshot.plugin_status or "").lower()
        if plug in ("crashed", "failed", "error"):
            _raise("PLUGIN_CRASH", f"Plugin status: {snapshot.plugin_status}")
        if snapshot.network_available is False:
            _raise("AGENT_OFFLINE", "Network unavailable", severity=AlertSeverity.CRITICAL)
        return raised

    @staticmethod
    def _serialize(alert: AlertEvent) -> dict[str, Any]:
        return {
            "id": str(alert.id),
            "correlation_id": str(alert.correlation_id) if alert.correlation_id else None,
            "department_id": str(alert.department_id) if alert.department_id else None,
            "building_id": str(alert.building_id) if alert.building_id else None,
            "agent_id": str(alert.sync_agent_id) if alert.sync_agent_id else None,
            "equipment_id": str(alert.equipment_id) if alert.equipment_id else None,
            "rule_code": alert.rule_code,
            "category": alert.category,
            "severity": alert.severity,
            "status": alert.status,
            "title": alert.title,
            "message": alert.message,
            "resolution": alert.resolution,
            "details": alert.details or {},
            "acknowledged_by": alert.acknowledged_by,
            "acknowledged_at": alert.acknowledged_at.isoformat() if alert.acknowledged_at else None,
            "resolved_by": alert.resolved_by,
            "resolved_at": alert.resolved_at.isoformat() if alert.resolved_at else None,
            "expires_at": alert.expires_at.isoformat() if alert.expires_at else None,
            "created_at": alert.created_at.isoformat() if alert.created_at else None,
            "updated_at": alert.updated_at.isoformat() if alert.updated_at else None,
        }
