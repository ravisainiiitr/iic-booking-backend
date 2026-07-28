"""Enterprise monitoring orchestration and telemetry ingest (Milestone 15)."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import (
    AgentHealthSnapshot,
    AgentPerformanceMetric,
    AgentUploadSession,
    AgentUploadSessionStatus,
    ResultProcessingQueue,
    ResultProcessingStatus,
    SyncLog,
    SyncLogCategory,
    SyncLogSeverity,
)
from iic_booking.sync.services.alerts import AlertService
from iic_booking.sync.services.capacity import CapacityService
from iic_booking.sync.services.health import HealthService
from iic_booking.sync.services.history import HistoryService
from iic_booking.sync.services.logging import write_sync_log
from iic_booking.sync.services.performance import PerformanceService

logger = logging.getLogger(__name__)


def _parse_ts(raw) -> timezone.datetime:
    if raw is None:
        return timezone.now()
    if hasattr(raw, "isoformat"):
        return raw if timezone.is_aware(raw) else timezone.make_aware(raw)
    parsed = parse_datetime(str(raw))
    if parsed is None:
        return timezone.now()
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


class MonitoringService:
    """Read-only enterprise monitoring facade. Never mutates sync pipelines."""

    def ingest_telemetry(self, sync_agent, payload: dict[str, Any], *, correlation_id=None) -> dict[str, Any]:
        corr = correlation_id or payload.get("correlation_id") or uuid.uuid4()
        if isinstance(corr, str):
            try:
                corr = uuid.UUID(corr)
            except ValueError:
                corr = uuid.uuid4()

        health = payload.get("health") or {}
        reported_at = _parse_ts(health.get("reported_at") or payload.get("reported_at"))
        metrics = dict(health.get("metrics") or payload.get("metrics") or {})

        snap = AgentHealthSnapshot.objects.create(
            sync_agent=sync_agent,
            department=getattr(sync_agent, "department", None),
            building=getattr(sync_agent, "building", None),
            reported_at=reported_at,
            overall_status=(health.get("overall_status") or "")[:32],
            overall_severity=(health.get("overall_severity") or "")[:32],
            cpu_percent=health.get("cpu_percent"),
            memory_mb=health.get("memory_mb"),
            memory_percent=health.get("memory_percent"),
            disk_used_percent=health.get("disk_used_percent"),
            disk_free_bytes=health.get("disk_free_bytes"),
            sqlite_size_bytes=health.get("sqlite_size_bytes"),
            upload_queue_size=int(health.get("upload_queue_size") or 0),
            processing_queue_size=int(health.get("processing_queue_size") or 0),
            discovery_queue_size=int(health.get("discovery_queue_size") or 0),
            heartbeat_latency_ms=health.get("heartbeat_latency_ms"),
            portal_latency_ms=health.get("portal_latency_ms"),
            upload_rate=health.get("upload_rate"),
            processing_rate=health.get("processing_rate"),
            recovery_state=(health.get("recovery_state") or "")[:64],
            security_status=(health.get("security_status") or "")[:64],
            plugin_status=(health.get("plugin_status") or "")[:64],
            network_available=health.get("network_available"),
            running_workers=int(health.get("running_workers") or 0),
            uptime_seconds=health.get("uptime_seconds"),
            agent_version=(health.get("agent_version") or "")[:64],
            schema_version=health.get("schema_version"),
            metrics=metrics,
            correlation_id=corr,
        )

        perf_rows = payload.get("performance") or []
        created_perf = 0
        for row in perf_rows[:200]:
            if not isinstance(row, dict):
                continue
            name = (row.get("name") or "").strip()
            if not name:
                continue
            AgentPerformanceMetric.objects.create(
                sync_agent=sync_agent,
                department=getattr(sync_agent, "department", None),
                category=(row.get("category") or "general")[:64],
                name=name[:128],
                value=row.get("value"),
                unit=(row.get("unit") or "")[:32],
                reported_at=_parse_ts(row.get("reported_at") or reported_at),
                details=row.get("details") or {},
            )
            created_perf += 1

        capacity_payload = payload.get("capacity") or {}
        capacity = None
        if capacity_payload or payload.get("include_capacity", True):
            capacity = CapacityService().capture(
                department_id=getattr(sync_agent, "department_id", None),
                sync_agent=sync_agent,
                metrics={
                    **metrics,
                    **capacity_payload,
                    "sqlite_size_bytes": health.get("sqlite_size_bytes"),
                    "peak_queue": (
                        int(health.get("upload_queue_size") or 0)
                        + int(health.get("processing_queue_size") or 0)
                        + int(health.get("discovery_queue_size") or 0)
                    ),
                    "plugin_count": int(
                        capacity_payload.get("plugin_count")
                        or metrics.get("plugin_count")
                        or 0
                    ),
                },
            )

        alerts = AlertService().evaluate_health_snapshot(snap, metrics=metrics)
        AlertService().expire_stale()

        # Light historical rollup (hourly) — read-only relative to sync engines.
        try:
            HistoryService().aggregate_from_snapshots(
                department_id=getattr(sync_agent, "department_id", None)
            )
        except Exception:
            logger.exception("monitoring.historical_aggregation failed")

        write_sync_log(
            event_code="MON-INGEST",
            category=SyncLogCategory.MONITORING,
            severity=SyncLogSeverity.INFO,
            message="Monitoring telemetry ingested",
            sync_agent=sync_agent,
            correlation_id=corr,
            json_payload={
                "snapshot_id": snap.id,
                "performance_count": created_perf,
                "alerts_raised": len(alerts),
            },
        )
        logger.info(
            "monitoring.health_collection agent=%s snapshot=%s alerts=%s",
            sync_agent.id,
            snap.id,
            len(alerts),
        )
        return {
            "snapshot_id": snap.id,
            "performance_count": created_perf,
            "capacity_id": capacity.get("id") if capacity else None,
            "alerts": [str(a.id) for a in alerts],
            "correlation_id": str(corr),
        }

    def overview(self, *, department_id=None) -> dict[str, Any]:
        health = HealthService().department_rollup(department_id=department_id)
        alerts = AlertService().summary(department_id=department_id)
        capacity = CapacityService().summary(department_id=department_id)
        performance = PerformanceService().rollup(department_id=department_id, hours=24)

        from iic_booking.sync.services.agent_registry import AgentRegistryService

        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        agent_ids = list(agents.values_list("id", flat=True))
        cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())

        pending_uploads = AgentUploadSession.objects.filter(
            sync_agent_id__in=agent_ids,
            status__in=[
                AgentUploadSessionStatus.PENDING,
                AgentUploadSessionStatus.RECEIVING,
            ],
        ).count()
        failed_uploads = AgentUploadSession.objects.filter(
            sync_agent_id__in=agent_ids,
            status=AgentUploadSessionStatus.FAILED,
            updated_at__gte=timezone.now() - timedelta(hours=24),
        ).count()
        pending_processing = ResultProcessingQueue.objects.filter(
            sync_agent_id__in=agent_ids,
            status__in=[
                ResultProcessingStatus.PENDING,
                ResultProcessingStatus.VALIDATING,
                ResultProcessingStatus.PARSING,
                ResultProcessingStatus.IMPORTING,
            ],
        ).count()
        failed_processing = ResultProcessingQueue.objects.filter(
            sync_agent_id__in=agent_ids,
            status=ResultProcessingStatus.FAILED,
            updated_at__gte=timezone.now() - timedelta(hours=24),
        ).count()

        recent_activity = list(
            SyncLog.objects.filter(
                sync_agent_id__in=agent_ids,
                created_at__gte=timezone.now() - timedelta(hours=6),
            )
            .order_by("-created_at")
            .values("event_code", "message", "severity", "category", "created_at")[:25]
        )
        for row in recent_activity:
            if row.get("created_at"):
                row["created_at"] = row["created_at"].isoformat()

        top_failures = list(
            SyncLog.objects.filter(
                sync_agent_id__in=agent_ids,
                severity__in=[SyncLogSeverity.ERROR, SyncLogSeverity.CRITICAL],
                created_at__gte=timezone.now() - timedelta(days=1),
            )
            .values("event_code")
            .annotate(c=Count("id"))
            .order_by("-c")[:10]
        )

        return {
            "overall_health": health,
            "department_health": health,
            "alerts": alerts,
            "queues": {
                "uploads_pending": pending_uploads,
                "uploads_failed_24h": failed_uploads,
                "processing_pending": pending_processing,
                "processing_failed_24h": failed_processing,
            },
            "upload_status": {
                "pending": pending_uploads,
                "failed_24h": failed_uploads,
            },
            "processing_status": {
                "pending": pending_processing,
                "failed_24h": failed_processing,
            },
            "capacity": capacity,
            "performance": performance,
            "resource_utilization": {
                "avg_cpu_percent": health.get("avg_cpu_percent"),
                "avg_memory_percent": health.get("avg_memory_percent"),
                "avg_disk_used_percent": health.get("avg_disk_used_percent"),
            },
            "top_failures": top_failures,
            "recent_activity": recent_activity,
            "agents_online": agents.filter(last_heartbeat_at__gte=cutoff).count(),
            "agents_total": agents.count(),
            "plugin_health": HealthService().latest_snapshots(department_id=department_id, limit=50),
            "generated_at": timezone.now().isoformat(),
        }

    def agents(self, *, department_id=None, building_id=None) -> list[dict[str, Any]]:
        from iic_booking.sync.services.agent_registry import AgentRegistryService

        agents = AgentRegistryService().list_agents(
            department_id=department_id,
            building_id=building_id,
        )
        health_svc = HealthService()
        enriched = []
        for row in agents:
            agent_id = row.get("id")
            from iic_booking.sync.models import DepartmentSyncAgent

            agent = DepartmentSyncAgent.objects.filter(pk=agent_id).first()
            if agent is None:
                enriched.append(row)
                continue
            detail = health_svc.agent_health(agent)
            enriched.append({**row, "monitoring": detail})
        return enriched
