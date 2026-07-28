"""Historical metric aggregation (Milestone 15)."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db.models import Avg, Count, Max, Min, Sum
from django.utils import timezone

from iic_booking.sync.models import (
    AgentHealthSnapshot,
    AgentPerformanceMetric,
    HistoricalMetric,
    HistoricalMetricPeriod,
)
from iic_booking.sync.services.agent_registry import AgentRegistryService

PERIOD_DELTAS = {
    HistoricalMetricPeriod.FIVE_MIN: timedelta(minutes=5),
    HistoricalMetricPeriod.FIFTEEN_MIN: timedelta(minutes=15),
    HistoricalMetricPeriod.HOURLY: timedelta(hours=1),
    HistoricalMetricPeriod.DAILY: timedelta(days=1),
    HistoricalMetricPeriod.WEEKLY: timedelta(days=7),
    HistoricalMetricPeriod.MONTHLY: timedelta(days=30),
}


def _floor_bucket(dt: datetime, period: str) -> datetime:
    dt = dt.astimezone(timezone.get_current_timezone()) if timezone.is_aware(dt) else timezone.make_aware(dt)
    if period == HistoricalMetricPeriod.FIVE_MIN:
        minute = (dt.minute // 5) * 5
        return dt.replace(minute=minute, second=0, microsecond=0)
    if period == HistoricalMetricPeriod.FIFTEEN_MIN:
        minute = (dt.minute // 15) * 15
        return dt.replace(minute=minute, second=0, microsecond=0)
    if period == HistoricalMetricPeriod.HOURLY:
        return dt.replace(minute=0, second=0, microsecond=0)
    if period == HistoricalMetricPeriod.DAILY:
        return dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == HistoricalMetricPeriod.WEEKLY:
        start = dt - timedelta(days=dt.weekday())
        return start.replace(hour=0, minute=0, second=0, microsecond=0)
    # monthly
    return dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class HistoryService:
    def query(
        self,
        *,
        department_id=None,
        agent_id=None,
        period: str | None = None,
        metric_name: str | None = None,
        days: int = 7,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        qs = HistoricalMetric.objects.filter(
            period_end__gte=timezone.now() - timedelta(days=max(1, days))
        ).select_related("sync_agent", "department", "building")
        if department_id:
            qs = qs.filter(department_id=department_id)
        if agent_id:
            qs = qs.filter(sync_agent_id=agent_id)
        if period:
            qs = qs.filter(period=period)
        if metric_name:
            qs = qs.filter(metric_name=metric_name)
        return [
            {
                "id": h.id,
                "department_id": str(h.department_id) if h.department_id else None,
                "building_id": str(h.building_id) if h.building_id else None,
                "agent_id": str(h.sync_agent_id) if h.sync_agent_id else None,
                "period": h.period,
                "metric_name": h.metric_name,
                "period_start": h.period_start.isoformat(),
                "period_end": h.period_end.isoformat(),
                "sample_count": h.sample_count,
                "min_value": h.min_value,
                "max_value": h.max_value,
                "avg_value": h.avg_value,
                "sum_value": h.sum_value,
                "last_value": h.last_value,
                "details": h.details or {},
            }
            for h in qs.order_by("-period_end")[: max(1, min(limit, 1000))]
        ]

    def aggregate_from_snapshots(
        self,
        *,
        department_id=None,
        period: str = HistoricalMetricPeriod.HOURLY,
    ) -> int:
        """Roll recent health snapshots into HistoricalMetric buckets."""
        if period not in PERIOD_DELTAS:
            period = HistoricalMetricPeriod.HOURLY
        delta = PERIOD_DELTAS[period]
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        since = timezone.now() - (delta * 3)
        written = 0
        metric_fields = [
            ("cpu_percent", "cpu_percent"),
            ("memory_percent", "memory_percent"),
            ("disk_used_percent", "disk_used_percent"),
            ("upload_queue_size", "upload_queue_size"),
            ("processing_queue_size", "processing_queue_size"),
            ("portal_latency_ms", "portal_latency_ms"),
        ]
        for agent in agents:
            snaps = list(
                AgentHealthSnapshot.objects.filter(
                    sync_agent=agent, reported_at__gte=since
                ).order_by("reported_at")
            )
            if not snaps:
                continue
            buckets: dict[datetime, list] = {}
            for snap in snaps:
                start = _floor_bucket(snap.reported_at, period)
                buckets.setdefault(start, []).append(snap)
            for start, group in buckets.items():
                end = start + delta
                for field, name in metric_fields:
                    values = [getattr(s, field) for s in group if getattr(s, field) is not None]
                    if not values:
                        continue
                    HistoricalMetric.objects.update_or_create(
                        sync_agent=agent,
                        period=period,
                        metric_name=name,
                        period_start=start,
                        defaults={
                            "department": getattr(agent, "department", None),
                            "building": getattr(agent, "building", None),
                            "period_end": end,
                            "sample_count": len(values),
                            "min_value": min(values),
                            "max_value": max(values),
                            "avg_value": sum(values) / len(values),
                            "sum_value": sum(values),
                            "last_value": values[-1],
                        },
                    )
                    written += 1
        return written

    def prune(self, *, retention_days: int = 90) -> int:
        cutoff = timezone.now() - timedelta(days=max(1, retention_days))
        deleted, _ = HistoricalMetric.objects.filter(period_end__lt=cutoff).delete()
        AgentPerformanceMetric.objects.filter(reported_at__lt=cutoff).delete()
        AgentHealthSnapshot.objects.filter(reported_at__lt=cutoff).delete()
        return deleted
