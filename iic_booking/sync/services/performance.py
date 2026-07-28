"""Performance metric queries (Milestone 15)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count, Max
from django.utils import timezone

from iic_booking.sync.models import AgentPerformanceMetric
from iic_booking.sync.services.agent_registry import AgentRegistryService


class PerformanceService:
    def recent(
        self,
        *,
        department_id=None,
        agent_id=None,
        category: str | None = None,
        hours: int = 24,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        qs = AgentPerformanceMetric.objects.filter(
            sync_agent_id__in=agents.values_list("id", flat=True),
            reported_at__gte=timezone.now() - timedelta(hours=max(1, hours)),
        ).select_related("sync_agent")
        if agent_id:
            qs = qs.filter(sync_agent_id=agent_id)
        if category:
            qs = qs.filter(category=category)
        return [
            {
                "id": m.id,
                "agent_id": str(m.sync_agent_id),
                "category": m.category,
                "name": m.name,
                "value": m.value,
                "unit": m.unit,
                "reported_at": m.reported_at.isoformat() if m.reported_at else None,
                "details": m.details or {},
            }
            for m in qs.order_by("-reported_at")[: max(1, min(limit, 1000))]
        ]

    def rollup(self, *, department_id=None, hours: int = 24) -> dict[str, Any]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        qs = AgentPerformanceMetric.objects.filter(
            sync_agent_id__in=agents.values_list("id", flat=True),
            reported_at__gte=timezone.now() - timedelta(hours=max(1, hours)),
        )
        by_category = list(
            qs.values("category").annotate(
                samples=Count("id"),
                avg_value=Avg("value"),
                max_value=Max("value"),
            )
        )
        return {
            "hours": hours,
            "categories": by_category,
            "sample_count": qs.count(),
            "generated_at": timezone.now().isoformat(),
        }
