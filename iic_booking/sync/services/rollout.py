"""Staged rollout orchestration (Milestone 16)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from django.utils import timezone

from iic_booking.sync.models import (
    DepartmentSyncAgent,
    ReleaseChannel,
    RolloutStrategy,
    UpdateDeployment,
    UpdateDeploymentStatus,
)
from iic_booking.sync.services.agent_registry import AgentRegistryService
from iic_booking.sync.services.update_audit import UpdateAuditService


class RolloutService:
    def create_deployment(
        self,
        *,
        package,
        strategy: str = RolloutStrategy.MANUAL,
        channel: str | None = None,
        percentage: int = 100,
        department_id=None,
        building_id=None,
        agent_group_id=None,
        scheduled_at=None,
        maintenance_window_start=None,
        maintenance_window_end=None,
        requires_approval: bool = False,
        target_agent_ids: list | None = None,
        created_by: str = "",
        correlation_id=None,
    ) -> dict[str, Any]:
        deployment = UpdateDeployment.objects.create(
            package=package,
            strategy=strategy,
            status=UpdateDeploymentStatus.PENDING,
            channel=channel or package.channel or ReleaseChannel.PRODUCTION,
            percentage=max(0, min(100, int(percentage or 100))),
            department_id=department_id,
            building_id=building_id,
            agent_group_id=agent_group_id,
            scheduled_at=scheduled_at,
            maintenance_window_start=maintenance_window_start,
            maintenance_window_end=maintenance_window_end,
            requires_approval=requires_approval,
            target_agent_ids=target_agent_ids or [],
            progress={"eligible": 0, "completed": 0, "failed": 0},
            correlation_id=correlation_id or uuid.uuid4(),
            created_by=created_by or "",
        )
        UpdateAuditService().write(
            event_code="UPD-DEPLOY",
            message=f"Deployment created for {package.version}",
            correlation_id=deployment.correlation_id,
            department_id=department_id,
            building_id=building_id,
            version=package.version,
            details={"deployment_id": str(deployment.id), "strategy": strategy},
        )
        return self.serialize(deployment)

    def start(self, deployment: UpdateDeployment, *, approved_by: str = "") -> dict[str, Any]:
        if deployment.requires_approval and not (approved_by or deployment.approved_by):
            raise ValueError("Deployment requires approval.")
        if approved_by:
            deployment.approved_by = approved_by
            deployment.approved_at = timezone.now()
        deployment.status = UpdateDeploymentStatus.IN_PROGRESS
        deployment.started_at = timezone.now()
        eligible = self.resolve_targets(deployment)
        deployment.target_agent_ids = [str(a.id) for a in eligible]
        deployment.progress = {
            "eligible": len(eligible),
            "completed": 0,
            "failed": 0,
            "started_at": deployment.started_at.isoformat(),
        }
        deployment.save()
        UpdateAuditService().write(
            event_code="UPD-DEPLOY-START",
            message=f"Deployment started ({len(eligible)} agents)",
            correlation_id=deployment.correlation_id,
            department_id=deployment.department_id,
            building_id=deployment.building_id,
            version=deployment.package.version,
            details={"deployment_id": str(deployment.id)},
        )
        return self.serialize(deployment)

    def resolve_targets(self, deployment: UpdateDeployment) -> list[DepartmentSyncAgent]:
        agents = list(
            AgentRegistryService().scoped_agents(
                department_id=deployment.department_id,
                building_id=deployment.building_id,
            )
        )
        # Channel filter
        channel = deployment.channel or deployment.package.channel
        agents = [a for a in agents if (a.update_channel or ReleaseChannel.PRODUCTION) == channel]

        if deployment.strategy == RolloutStrategy.IMMEDIATE:
            return agents
        if deployment.strategy == RolloutStrategy.MANUAL:
            ids = {str(x) for x in (deployment.target_agent_ids or [])}
            if not ids:
                return []
            return [a for a in agents if str(a.id) in ids]
        if deployment.strategy == RolloutStrategy.AGENT_GROUP and deployment.agent_group_id:
            from iic_booking.sync.models import SyncAgentAssignment

            assigned = set(
                SyncAgentAssignment.objects.filter(
                    group_id=deployment.agent_group_id, is_active=True
                ).values_list("sync_agent_id", flat=True)
            )
            return [a for a in agents if a.id in assigned]
        if deployment.strategy == RolloutStrategy.PERCENTAGE:
            pct = max(0, min(100, deployment.percentage or 100))
            n = max(1, int(len(agents) * pct / 100.0)) if agents and pct else 0
            # Stable subset by agent UUID
            ordered = sorted(agents, key=lambda a: str(a.id))
            return ordered[:n]
        # DEPARTMENT / BUILDING already scoped
        return agents

    def is_in_maintenance_window(self, deployment: UpdateDeployment, now: datetime | None = None) -> bool:
        now = now or timezone.now()
        start = deployment.maintenance_window_start
        end = deployment.maintenance_window_end
        if start is None and end is None:
            return True
        if start and now < start:
            return False
        if end and now > end:
            return False
        return True

    def agent_is_targeted(self, deployment: UpdateDeployment, agent: DepartmentSyncAgent) -> bool:
        if deployment.status not in (
            UpdateDeploymentStatus.IN_PROGRESS,
            UpdateDeploymentStatus.PENDING,
        ):
            return False
        if not self.is_in_maintenance_window(deployment):
            return False
        targets = {str(x) for x in (deployment.target_agent_ids or [])}
        if targets:
            return str(agent.id) in targets
        return agent in self.resolve_targets(deployment)

    def progress(self, deployment: UpdateDeployment) -> dict[str, Any]:
        return self.serialize(deployment)

    @staticmethod
    def serialize(deployment: UpdateDeployment) -> dict[str, Any]:
        return {
            "id": str(deployment.id),
            "package_id": str(deployment.package_id),
            "package_version": deployment.package.version if deployment.package_id else None,
            "strategy": deployment.strategy,
            "status": deployment.status,
            "channel": deployment.channel,
            "percentage": deployment.percentage,
            "department_id": str(deployment.department_id) if deployment.department_id else None,
            "building_id": str(deployment.building_id) if deployment.building_id else None,
            "agent_group_id": str(deployment.agent_group_id) if deployment.agent_group_id else None,
            "scheduled_at": deployment.scheduled_at.isoformat() if deployment.scheduled_at else None,
            "maintenance_window_start": deployment.maintenance_window_start.isoformat()
            if deployment.maintenance_window_start
            else None,
            "maintenance_window_end": deployment.maintenance_window_end.isoformat()
            if deployment.maintenance_window_end
            else None,
            "requires_approval": deployment.requires_approval,
            "approved_by": deployment.approved_by,
            "target_agent_ids": deployment.target_agent_ids or [],
            "progress": deployment.progress or {},
            "correlation_id": str(deployment.correlation_id) if deployment.correlation_id else None,
            "created_at": deployment.created_at.isoformat() if deployment.created_at else None,
            "started_at": deployment.started_at.isoformat() if deployment.started_at else None,
            "completed_at": deployment.completed_at.isoformat() if deployment.completed_at else None,
        }
