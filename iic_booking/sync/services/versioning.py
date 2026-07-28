"""Version compatibility and distribution (Milestone 16)."""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.db.models import Count
from django.utils import timezone

from iic_booking.sync.models import (
    ConfigurationVersion,
    DepartmentSyncAgent,
    PluginRelease,
    ReleasePackage,
    ReleasePackageStatus,
    UpdateHistory,
    UpdateLifecycleState,
)
from iic_booking.sync.services.agent_registry import AgentRegistryService


def _parse_semver_parts(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for token in (version or "").replace("-", ".").split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def is_version_gte(current: str, minimum: str) -> bool:
    if not minimum:
        return True
    return _parse_semver_parts(current) >= _parse_semver_parts(minimum)


class VersioningService:
    def agent_versions(self, *, department_id=None) -> dict[str, Any]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        counts = Counter((a.version or "unknown") for a in agents)
        return {
            "distribution": [{"version": k, "count": v} for k, v in counts.most_common()],
            "total_agents": agents.count(),
            "generated_at": timezone.now().isoformat(),
        }

    def compatibility_check(
        self,
        package: ReleasePackage,
        *,
        agent_version: str = "",
        schema_version: int | None = None,
        security_version: int | None = None,
        recovery_version: int | None = None,
    ) -> dict[str, Any]:
        reasons: list[str] = []
        if package.min_agent_version and not is_version_gte(agent_version, package.min_agent_version):
            reasons.append(
                f"Agent version {agent_version} < minimum {package.min_agent_version}"
            )
        if package.min_schema_version is not None and (
            schema_version is None or schema_version < package.min_schema_version
        ):
            reasons.append(
                f"Schema version {schema_version} < minimum {package.min_schema_version}"
            )
        if package.security_version is not None and security_version is not None:
            if security_version < package.security_version:
                reasons.append("Security version below package requirement")
        if package.recovery_version is not None and recovery_version is not None:
            if recovery_version < package.recovery_version:
                reasons.append("Recovery version below package requirement")
        return {
            "compatible": len(reasons) == 0,
            "reasons": reasons,
            "package_version": package.version,
            "package_type": package.package_type,
        }

    def history(self, *, department_id=None, limit: int = 50) -> list[dict[str, Any]]:
        qs = ReleasePackage.objects.all().order_by("-created_at")
        if department_id:
            qs = qs.filter(department_id=department_id)
        rows = []
        for pkg in qs[: max(1, min(limit, 200))]:
            rows.append(
                {
                    "id": str(pkg.id),
                    "package_type": pkg.package_type,
                    "version": pkg.version,
                    "channel": pkg.channel,
                    "status": pkg.status,
                    "published_at": pkg.published_at.isoformat() if pkg.published_at else None,
                    "created_at": pkg.created_at.isoformat() if pkg.created_at else None,
                }
            )
        return rows

    def active_configuration(self, *, department_id=None) -> dict[str, Any] | None:
        qs = ConfigurationVersion.objects.filter(is_active=True)
        if department_id:
            qs = qs.filter(department_id=department_id)
        row = qs.order_by("-published_at", "-created_at").first()
        if row is None:
            return None
        return {
            "id": str(row.id),
            "version_label": row.version_label,
            "content_hash": row.content_hash,
            "content": row.content or {},
            "department_id": str(row.department_id) if row.department_id else None,
            "published_at": row.published_at.isoformat() if row.published_at else None,
        }

    def plugin_releases(self, *, plugin_id: str | None = None) -> list[dict[str, Any]]:
        qs = PluginRelease.objects.select_related("package").order_by("-created_at")
        if plugin_id:
            qs = qs.filter(plugin_id=plugin_id)
        return [
            {
                "id": str(p.id),
                "plugin_id": p.plugin_id,
                "plugin_name": p.plugin_name,
                "plugin_version": p.plugin_version,
                "package_id": str(p.package_id),
                "package_status": p.package.status,
                "supports_hot_reload": p.supports_hot_reload,
                "requires_agent_restart": p.requires_agent_restart,
            }
            for p in qs[:100]
        ]

    def pending_and_failed(self, *, department_id=None) -> dict[str, Any]:
        agents = AgentRegistryService().scoped_agents(department_id=department_id)
        agent_ids = list(agents.values_list("id", flat=True))
        pending = UpdateHistory.objects.filter(
            sync_agent_id__in=agent_ids,
            state__in=[
                UpdateLifecycleState.AVAILABLE,
                UpdateLifecycleState.DOWNLOADING,
                UpdateLifecycleState.VERIFYING,
                UpdateLifecycleState.READY,
                UpdateLifecycleState.INSTALLING,
                UpdateLifecycleState.VALIDATING,
            ],
        ).count()
        failed = UpdateHistory.objects.filter(
            sync_agent_id__in=agent_ids,
            state=UpdateLifecycleState.FAILED,
        ).count()
        completed = UpdateHistory.objects.filter(
            sync_agent_id__in=agent_ids,
            state=UpdateLifecycleState.COMPLETED,
        ).count()
        published = ReleasePackage.objects.filter(status=ReleasePackageStatus.PUBLISHED).count()
        return {
            "pending_updates": pending,
            "failed_updates": failed,
            "completed_updates": completed,
            "published_releases": published,
        }
