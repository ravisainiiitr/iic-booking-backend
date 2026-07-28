"""Agent-facing update manager and dashboard (Milestone 16)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count
from django.utils import timezone

from iic_booking.sync.models import (
    ReleaseChannel,
    ReleaseManifest,
    ReleasePackage,
    ReleasePackageStatus,
    RollbackHistory,
    SyncLogSeverity,
    UpdateDeployment,
    UpdateDeploymentStatus,
    UpdateHistory,
    UpdateLifecycleState,
)
from iic_booking.sync.services.package_signing import PackageSigningService
from iic_booking.sync.services.releases import ReleaseService
from iic_booking.sync.services.rollout import RolloutService
from iic_booking.sync.services.update_audit import UpdateAuditService
from iic_booking.sync.services.versioning import VersioningService, is_version_gte


class UpdateManagerService:
    def discover_for_agent(self, sync_agent, *, current_version: str = "") -> dict[str, Any]:
        channel = sync_agent.update_channel or ReleaseChannel.PRODUCTION
        agent_version = current_version or sync_agent.version or ""
        packages = (
            ReleasePackage.objects.filter(
                status=ReleasePackageStatus.PUBLISHED,
                channel=channel,
            )
            .order_by("-published_at", "-created_at")
        )
        # Department-scoped packages
        scoped = []
        for pkg in packages[:50]:
            if pkg.department_id and pkg.department_id != sync_agent.department_id:
                continue
            compat = VersioningService().compatibility_check(
                pkg,
                agent_version=agent_version,
                schema_version=sync_agent.last_reported_schema_version,
                security_version=sync_agent.security_version,
            )
            if not compat["compatible"] and pkg.package_type == "AGENT":
                # Still expose but mark incompatible
                pass
            # Prefer newer than current for agent packages
            if pkg.package_type == "AGENT" and agent_version and not is_version_gte(
                pkg.version, agent_version
            ):
                # package.version should be > current; skip if current already >= package
                if is_version_gte(agent_version, pkg.version):
                    continue
            manifest = pkg.manifests.filter(is_active=True).order_by("-created_at").first()
            scoped.append(
                {
                    **ReleaseService.serialize(pkg),
                    "compatible": compat["compatible"],
                    "compatibility_reasons": compat["reasons"],
                    "manifest": {
                        "id": str(manifest.id),
                        "document": manifest.document or {},
                        "document_sha256": manifest.document_sha256,
                        "signature": manifest.signature,
                    }
                    if manifest
                    else None,
                }
            )

        # Active deployments targeting this agent
        deployments = []
        for dep in UpdateDeployment.objects.filter(
            status=UpdateDeploymentStatus.IN_PROGRESS,
            channel=channel,
        ).select_related("package")[:20]:
            if RolloutService().agent_is_targeted(dep, sync_agent):
                deployments.append(RolloutService.serialize(dep))

        UpdateAuditService().write(
            event_code="UPD-DISCOVER",
            message="Update discovery completed",
            sync_agent=sync_agent,
            department_id=sync_agent.department_id,
            building_id=sync_agent.building_id,
            version=agent_version,
            details={"available": len(scoped), "deployments": len(deployments)},
        )
        return {
            "channel": channel,
            "current_version": agent_version,
            "available": scoped,
            "deployments": deployments,
            "policy": {
                "require_signature": True,
                "require_sha256": True,
                "trusted_publisher": "IIC Portal",
            },
            "generated_at": timezone.now().isoformat(),
        }

    def report_status(self, sync_agent, payload: dict[str, Any], *, correlation_id=None) -> dict[str, Any]:
        corr = correlation_id or payload.get("correlation_id") or uuid.uuid4()
        if isinstance(corr, str):
            try:
                corr = uuid.UUID(corr)
            except ValueError:
                corr = uuid.uuid4()

        package_id = payload.get("package_id")
        package = ReleasePackage.objects.filter(pk=package_id).first() if package_id else None
        deployment_id = payload.get("deployment_id")
        deployment = UpdateDeployment.objects.filter(pk=deployment_id).first() if deployment_id else None
        state = payload.get("state") or UpdateLifecycleState.AVAILABLE
        history_id = payload.get("history_id")
        history = UpdateHistory.objects.filter(pk=history_id, sync_agent=sync_agent).first() if history_id else None

        if history is None:
            history = UpdateHistory.objects.create(
                sync_agent=sync_agent,
                package=package,
                deployment=deployment,
                department=getattr(sync_agent, "department", None),
                building=getattr(sync_agent, "building", None),
                from_version=payload.get("from_version") or sync_agent.version or "",
                to_version=payload.get("to_version") or (package.version if package else ""),
                state=state,
                package_type=payload.get("package_type") or (package.package_type if package else ""),
                message=(payload.get("message") or "")[:500],
                download_bytes=int(payload.get("download_bytes") or 0),
                download_ms=payload.get("download_ms"),
                install_ms=payload.get("install_ms"),
                validation_ms=payload.get("validation_ms"),
                details=payload.get("details") or {},
                correlation_id=corr,
            )
        else:
            history.state = state
            history.message = (payload.get("message") or history.message)[:500]
            history.download_bytes = int(payload.get("download_bytes") or history.download_bytes or 0)
            if payload.get("download_ms") is not None:
                history.download_ms = payload.get("download_ms")
            if payload.get("install_ms") is not None:
                history.install_ms = payload.get("install_ms")
            if payload.get("validation_ms") is not None:
                history.validation_ms = payload.get("validation_ms")
            history.details = {**(history.details or {}), **(payload.get("details") or {})}
            if state in (
                UpdateLifecycleState.COMPLETED,
                UpdateLifecycleState.FAILED,
                UpdateLifecycleState.ROLLED_BACK,
                UpdateLifecycleState.CANCELLED,
            ):
                history.completed_at = timezone.now()
            history.save()

        if state == UpdateLifecycleState.COMPLETED and package and package.package_type == "AGENT":
            sync_agent.version = package.version
            sync_agent.save(update_fields=["version", "updated_at"])

        if state == UpdateLifecycleState.FAILED and payload.get("auto_rollback"):
            self._record_rollback(
                sync_agent,
                package=package,
                history=history,
                reason=payload.get("rollback_reason") or "Automatic rollback after failed validation",
                automatic=True,
                correlation_id=corr,
                from_version=history.to_version,
                to_version=history.from_version,
            )
            history.state = UpdateLifecycleState.ROLLED_BACK
            history.completed_at = timezone.now()
            history.save(update_fields=["state", "completed_at"])

        if deployment and state in (
            UpdateLifecycleState.COMPLETED,
            UpdateLifecycleState.FAILED,
            UpdateLifecycleState.ROLLED_BACK,
        ):
            progress = dict(deployment.progress or {})
            key = "completed" if state == UpdateLifecycleState.COMPLETED else "failed"
            progress[key] = int(progress.get(key) or 0) + 1
            deployment.progress = progress
            eligible = int(progress.get("eligible") or 0)
            done = int(progress.get("completed") or 0) + int(progress.get("failed") or 0)
            if eligible and done >= eligible:
                deployment.status = UpdateDeploymentStatus.COMPLETED
                deployment.completed_at = timezone.now()
            deployment.save()

        UpdateAuditService().write(
            event_code="UPD-STATUS",
            message=f"Update status {state}",
            sync_agent=sync_agent,
            correlation_id=corr,
            department_id=sync_agent.department_id,
            building_id=sync_agent.building_id,
            version=history.to_version,
            details={"history_id": str(history.id), "state": state},
            severity=SyncLogSeverity.ERROR
            if state == UpdateLifecycleState.FAILED
            else SyncLogSeverity.INFO,
        )
        return {
            "history_id": str(history.id),
            "state": history.state,
            "correlation_id": str(corr),
        }

    def rollback(
        self,
        *,
        package_id=None,
        agent_id=None,
        reason: str = "",
        user_name: str = "",
        correlation_id=None,
        to_version: str = "",
    ) -> dict[str, Any]:
        from iic_booking.sync.models import DepartmentSyncAgent

        agent = DepartmentSyncAgent.objects.filter(pk=agent_id).first() if agent_id else None
        package = ReleasePackage.objects.filter(pk=package_id).first() if package_id else None
        history = None
        if agent:
            history = (
                UpdateHistory.objects.filter(sync_agent=agent)
                .order_by("-started_at")
                .first()
            )
        row = self._record_rollback(
            agent,
            package=package,
            history=history,
            reason=reason or "Manual rollback",
            automatic=False,
            correlation_id=correlation_id or uuid.uuid4(),
            from_version=(history.to_version if history else (agent.version if agent else "")),
            to_version=to_version or (history.from_version if history else ""),
            created_by=user_name,
        )
        if agent and to_version:
            agent.version = to_version
            agent.save(update_fields=["version", "updated_at"])
        if history:
            history.state = UpdateLifecycleState.ROLLED_BACK
            history.completed_at = timezone.now()
            history.message = (reason or history.message)[:500]
            history.save(update_fields=["state", "completed_at", "message"])
        return row

    def history(self, *, department_id=None, agent_id=None, limit: int = 100) -> list[dict[str, Any]]:
        qs = UpdateHistory.objects.select_related("sync_agent", "package", "department").order_by(
            "-started_at"
        )
        if department_id:
            qs = qs.filter(department_id=department_id)
        if agent_id:
            qs = qs.filter(sync_agent_id=agent_id)
        rows = []
        for h in qs[: max(1, min(limit, 500))]:
            rows.append(
                {
                    "id": str(h.id),
                    "agent_id": str(h.sync_agent_id),
                    "package_id": str(h.package_id) if h.package_id else None,
                    "deployment_id": str(h.deployment_id) if h.deployment_id else None,
                    "from_version": h.from_version,
                    "to_version": h.to_version,
                    "state": h.state,
                    "package_type": h.package_type,
                    "message": h.message,
                    "download_bytes": h.download_bytes,
                    "download_ms": h.download_ms,
                    "install_ms": h.install_ms,
                    "validation_ms": h.validation_ms,
                    "correlation_id": str(h.correlation_id) if h.correlation_id else None,
                    "started_at": h.started_at.isoformat() if h.started_at else None,
                    "completed_at": h.completed_at.isoformat() if h.completed_at else None,
                }
            )
        return rows

    def status_dashboard(self, *, department_id=None) -> dict[str, Any]:
        versioning = VersioningService()
        pending = versioning.pending_and_failed(department_id=department_id)
        distribution = versioning.agent_versions(department_id=department_id)
        rollbacks = RollbackHistory.objects.all()
        if department_id:
            rollbacks = rollbacks.filter(department_id=department_id)
        recent_rollbacks = [
            {
                "id": str(r.id),
                "agent_id": str(r.sync_agent_id) if r.sync_agent_id else None,
                "from_version": r.from_version,
                "to_version": r.to_version,
                "reason": r.reason,
                "automatic": r.automatic,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rollbacks.order_by("-created_at")[:25]
        ]
        deployments = UpdateDeployment.objects.select_related("package").order_by("-created_at")[:25]
        if department_id:
            deployments = deployments.filter(department_id=department_id)[:25]
        hist = UpdateHistory.objects.all()
        if department_id:
            hist = hist.filter(department_id=department_id)
        since = timezone.now() - timedelta(days=30)
        hist30 = hist.filter(started_at__gte=since)
        success = hist30.filter(state=UpdateLifecycleState.COMPLETED).count()
        failed = hist30.filter(state=UpdateLifecycleState.FAILED).count()
        total = success + failed
        telemetry = hist30.aggregate(
            avg_download_ms=Avg("download_ms"),
            avg_install_ms=Avg("install_ms"),
            avg_validation_ms=Avg("validation_ms"),
        )
        channel_status = list(
            ReleasePackage.objects.filter(status=ReleasePackageStatus.PUBLISHED)
            .values("channel")
            .annotate(c=Count("id"))
        )
        return {
            "version_distribution": distribution,
            "pending_updates": pending.get("pending_updates"),
            "failed_updates": pending.get("failed_updates"),
            "completed_updates": pending.get("completed_updates"),
            "published_releases": pending.get("published_releases"),
            "rollback_history": recent_rollbacks,
            "deployments": [RolloutService.serialize(d) for d in deployments],
            "channel_status": channel_status,
            "release_health": {
                "success_rate": round((success / total) * 100, 1) if total else 100.0,
                "failure_rate": round((failed / total) * 100, 1) if total else 0.0,
                "samples_30d": total,
            },
            "telemetry": telemetry,
            "adoption": distribution.get("distribution"),
            "generated_at": timezone.now().isoformat(),
        }

    def verify_manifest(self, manifest_id) -> dict[str, Any]:
        manifest = ReleaseManifest.objects.select_related("package").filter(pk=manifest_id).first()
        if manifest is None:
            raise ValueError("Manifest not found.")
        import json

        signer = PackageSigningService()
        raw = json.dumps(manifest.document or {}, sort_keys=True, separators=(",", ":"))
        ok_doc = signer.sha256_hex(raw) == (manifest.document_sha256 or "")
        ok_sig = signer.verify(raw, manifest.signature or "")
        pkg = manifest.package
        ok_pkg = signer.verify_package_fields(
            version=pkg.version,
            sha256=pkg.sha256,
            download_url=pkg.download_url,
            signature=pkg.signature,
        )
        return {
            "manifest_id": str(manifest.id),
            "document_integrity": ok_doc,
            "manifest_signature_valid": ok_sig,
            "package_signature_valid": ok_pkg,
            "valid": ok_doc and ok_sig and ok_pkg,
        }

    def _record_rollback(
        self,
        sync_agent,
        *,
        package=None,
        history=None,
        reason: str,
        automatic: bool,
        correlation_id,
        from_version: str = "",
        to_version: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        row = RollbackHistory.objects.create(
            sync_agent=sync_agent,
            package=package,
            update_history=history,
            department=getattr(sync_agent, "department", None) if sync_agent else None,
            from_version=from_version,
            to_version=to_version,
            reason=(reason or "")[:500],
            automatic=automatic,
            validated=True,
            correlation_id=correlation_id,
            created_by=created_by or "",
        )
        UpdateAuditService().write(
            event_code="UPD-ROLLBACK",
            message=f"Rollback: {from_version} → {to_version}",
            sync_agent=sync_agent,
            correlation_id=correlation_id,
            department_id=getattr(sync_agent, "department_id", None) if sync_agent else None,
            building_id=getattr(sync_agent, "building_id", None) if sync_agent else None,
            version=to_version,
            details={"rollback_id": str(row.id), "automatic": automatic, "reason": reason},
            severity=SyncLogSeverity.WARNING,
        )
        return {
            "id": str(row.id),
            "from_version": row.from_version,
            "to_version": row.to_version,
            "reason": row.reason,
            "automatic": row.automatic,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
