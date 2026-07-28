"""Health aggregation for enterprise monitoring (Milestone 15)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Avg, Max
from django.utils import timezone

from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import AgentHealthSnapshot, AgentHeartbeat, DepartmentSyncAgent
from iic_booking.sync.services.agent_registry import AgentRegistryService


class HealthService:
    def latest_snapshots(self, *, department_id=None, limit: int = 100) -> list[dict[str, Any]]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        agent_ids = list(agents.values_list("id", flat=True))
        seen: set = set()
        snaps = []
        for row in (
            AgentHealthSnapshot.objects.filter(sync_agent_id__in=agent_ids)
            .select_related("sync_agent", "department", "building")
            .order_by("-reported_at")
        ):
            if row.sync_agent_id in seen:
                continue
            seen.add(row.sync_agent_id)
            snaps.append(row)
            if len(snaps) >= limit:
                break
        return [self._serialize(s) for s in snaps]

    def agent_health(self, agent: DepartmentSyncAgent) -> dict[str, Any]:
        snap = (
            AgentHealthSnapshot.objects.filter(sync_agent=agent)
            .order_by("-reported_at")
            .first()
        )
        cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())
        online = bool(agent.last_heartbeat_at and agent.last_heartbeat_at >= cutoff)
        hb = (
            AgentHeartbeat.objects.filter(sync_agent=agent)
            .order_by("-reported_at")
            .first()
        )
        return {
            "agent_id": str(agent.id),
            "agent_name": agent.agent_name,
            "hostname": agent.machine_name or agent.agent_name,
            "status": agent.status,
            "online": online,
            "last_heartbeat_at": agent.last_heartbeat_at.isoformat() if agent.last_heartbeat_at else None,
            "latest_snapshot": self._serialize(snap) if snap else None,
            "latest_heartbeat": {
                "cpu_percent": getattr(hb, "cpu_percent", None),
                "memory_percent": getattr(hb, "memory_percent", None),
                "disk_percent": getattr(hb, "disk_percent", None),
                "reported_at": hb.reported_at.isoformat() if hb and hb.reported_at else None,
            }
            if hb
            else None,
        }

    def department_rollup(self, *, department_id=None) -> dict[str, Any]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        cutoff = timezone.now() - timedelta(seconds=heartbeat_timeout_seconds())
        total = agents.count()
        online = agents.filter(last_heartbeat_at__gte=cutoff).count()
        since = timezone.now() - timedelta(hours=1)
        agent_ids = list(agents.values_list("id", flat=True))
        agg = AgentHealthSnapshot.objects.filter(
            sync_agent_id__in=agent_ids, reported_at__gte=since
        ).aggregate(
            avg_cpu=Avg("cpu_percent"),
            avg_mem=Avg("memory_percent"),
            avg_disk=Avg("disk_used_percent"),
            max_upload_q=Max("upload_queue_size"),
            max_proc_q=Max("processing_queue_size"),
            avg_portal_latency=Avg("portal_latency_ms"),
        )
        return {
            "agents_total": total,
            "agents_online": online,
            "agents_offline": max(0, total - online),
            "health_score": round((online / total) * 100, 1) if total else 100.0,
            "avg_cpu_percent": agg.get("avg_cpu"),
            "avg_memory_percent": agg.get("avg_mem"),
            "avg_disk_used_percent": agg.get("avg_disk"),
            "peak_upload_queue": agg.get("max_upload_q") or 0,
            "peak_processing_queue": agg.get("max_proc_q") or 0,
            "avg_portal_latency_ms": agg.get("avg_portal_latency"),
            "generated_at": timezone.now().isoformat(),
        }

    @staticmethod
    def _serialize(snap: AgentHealthSnapshot | None) -> dict[str, Any] | None:
        if snap is None:
            return None
        return {
            "id": snap.id,
            "agent_id": str(snap.sync_agent_id),
            "department_id": str(snap.department_id) if snap.department_id else None,
            "building_id": str(snap.building_id) if snap.building_id else None,
            "reported_at": snap.reported_at.isoformat() if snap.reported_at else None,
            "overall_status": snap.overall_status,
            "overall_severity": snap.overall_severity,
            "cpu_percent": snap.cpu_percent,
            "memory_mb": snap.memory_mb,
            "memory_percent": snap.memory_percent,
            "disk_used_percent": snap.disk_used_percent,
            "disk_free_bytes": snap.disk_free_bytes,
            "sqlite_size_bytes": snap.sqlite_size_bytes,
            "upload_queue_size": snap.upload_queue_size,
            "processing_queue_size": snap.processing_queue_size,
            "discovery_queue_size": snap.discovery_queue_size,
            "heartbeat_latency_ms": snap.heartbeat_latency_ms,
            "portal_latency_ms": snap.portal_latency_ms,
            "upload_rate": snap.upload_rate,
            "processing_rate": snap.processing_rate,
            "recovery_state": snap.recovery_state,
            "security_status": snap.security_status,
            "plugin_status": snap.plugin_status,
            "network_available": snap.network_available,
            "running_workers": snap.running_workers,
            "uptime_seconds": snap.uptime_seconds,
            "agent_version": snap.agent_version,
            "schema_version": snap.schema_version,
            "metrics": snap.metrics or {},
            "correlation_id": str(snap.correlation_id) if snap.correlation_id else None,
        }
