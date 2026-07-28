"""Release package lifecycle (Milestone 16)."""

from __future__ import annotations

import json
import uuid
from typing import Any

from django.utils import timezone

from iic_booking.sync.models import (
    ConfigurationVersion,
    PluginRelease,
    ReleaseManifest,
    ReleasePackage,
    ReleasePackageStatus,
    ReleasePackageType,
    SyncLogSeverity,
)
from iic_booking.sync.services.package_signing import PackageSigningService
from iic_booking.sync.services.update_audit import UpdateAuditService


class ReleaseService:
    def list_releases(
        self,
        *,
        department_id=None,
        channel: str | None = None,
        package_type: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        qs = ReleasePackage.objects.select_related("department").order_by("-created_at")
        if department_id:
            qs = qs.filter(department_id=department_id)
        if channel:
            qs = qs.filter(channel=channel)
        if package_type:
            qs = qs.filter(package_type=package_type)
        if status:
            qs = qs.filter(status=status)
        return [self.serialize(p) for p in qs[: max(1, min(limit, 500))]]

    def get(self, package_id) -> dict[str, Any] | None:
        pkg = ReleasePackage.objects.filter(pk=package_id).first()
        if pkg is None:
            return None
        data = self.serialize(pkg)
        manifest = pkg.manifests.filter(is_active=True).order_by("-created_at").first()
        data["manifest"] = (
            {
                "id": str(manifest.id),
                "manifest_version": manifest.manifest_version,
                "document": manifest.document or {},
                "document_sha256": manifest.document_sha256,
                "signature": manifest.signature,
            }
            if manifest
            else None
        )
        if hasattr(pkg, "plugin_release"):
            try:
                pr = pkg.plugin_release
            except PluginRelease.DoesNotExist:
                pr = None
            if pr is not None:
                data["plugin"] = {
                    "plugin_id": pr.plugin_id,
                    "plugin_name": pr.plugin_name,
                    "plugin_version": pr.plugin_version,
                    "supports_hot_reload": pr.supports_hot_reload,
                    "requires_agent_restart": pr.requires_agent_restart,
                }
        return data

    def create(
        self,
        data: dict[str, Any],
        *,
        created_by: str = "",
        correlation_id=None,
    ) -> dict[str, Any]:
        signer = PackageSigningService()
        sha256 = (data.get("sha256") or "").strip().lower()
        version = (data.get("version") or "").strip()
        download_url = data.get("download_url") or ""
        if not sha256 and data.get("content"):
            sha256 = signer.sha256_hex(json.dumps(data["content"], sort_keys=True))
        signature = data.get("signature") or signer.sign_package_fields(
            version=version, sha256=sha256, download_url=download_url
        )
        pkg = ReleasePackage.objects.create(
            package_type=data.get("package_type") or ReleasePackageType.AGENT,
            channel=data.get("channel") or "PRODUCTION",
            version=version,
            display_name=data.get("display_name") or f"Release {version}",
            description=data.get("description") or "",
            status=ReleasePackageStatus.DRAFT,
            download_url=download_url,
            package_size_bytes=int(data.get("package_size_bytes") or 0),
            sha256=sha256,
            signature=signature,
            publisher=data.get("publisher") or "IIC Portal",
            min_agent_version=data.get("min_agent_version") or "",
            min_schema_version=data.get("min_schema_version"),
            security_version=data.get("security_version"),
            recovery_version=data.get("recovery_version"),
            api_version=data.get("api_version") or "",
            compatibility=data.get("compatibility") or {},
            dependencies=data.get("dependencies") or [],
            plugin_id=data.get("plugin_id") or "",
            department_id=data.get("department_id"),
            created_by=created_by or "",
        )
        if pkg.package_type == ReleasePackageType.PLUGIN or data.get("plugin_id"):
            PluginRelease.objects.create(
                package=pkg,
                plugin_id=data.get("plugin_id") or "unknown",
                plugin_name=data.get("plugin_name") or "",
                plugin_version=data.get("plugin_version") or version,
                supports_hot_reload=bool(data.get("supports_hot_reload", True)),
                requires_agent_restart=bool(data.get("requires_agent_restart", False)),
                min_agent_version=data.get("min_agent_version") or "",
                compatibility=data.get("compatibility") or {},
            )
        if pkg.package_type == ReleasePackageType.CONFIGURATION or data.get("content") is not None:
            content = data.get("content") or {}
            content_hash = signer.sha256_hex(json.dumps(content, sort_keys=True))
            ConfigurationVersion.objects.create(
                version_label=version,
                department_id=data.get("department_id"),
                package=pkg,
                content_hash=content_hash,
                content=content,
                is_active=False,
                created_by=created_by or "",
            )
            if not pkg.sha256:
                pkg.sha256 = content_hash
                pkg.signature = signer.sign_package_fields(
                    version=version, sha256=content_hash, download_url=download_url
                )
                pkg.save(update_fields=["sha256", "signature", "updated_at"])

        UpdateAuditService().write(
            event_code="UPD-CREATE",
            message=f"Release created: {pkg.package_type} {pkg.version}",
            correlation_id=correlation_id or uuid.uuid4(),
            department_id=pkg.department_id,
            version=pkg.version,
            details={"package_id": str(pkg.id)},
        )
        return self.serialize(pkg)

    def publish(self, package_id, *, user_name: str = "", correlation_id=None) -> dict[str, Any]:
        pkg = ReleasePackage.objects.filter(pk=package_id).first()
        if pkg is None:
            raise ValueError("Release not found.")
        signer = PackageSigningService()
        if not pkg.signature:
            pkg.signature = signer.sign_package_fields(
                version=pkg.version, sha256=pkg.sha256, download_url=pkg.download_url
            )
        if not signer.verify_package_fields(
            version=pkg.version,
            sha256=pkg.sha256,
            download_url=pkg.download_url,
            signature=pkg.signature,
        ):
            raise ValueError("Package signature invalid; cannot publish.")

        pkg.status = ReleasePackageStatus.PUBLISHED
        pkg.published_at = timezone.now()
        pkg.save(update_fields=["status", "published_at", "signature", "updated_at"])

        doc = signer.build_manifest_document(pkg)
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":"))
        doc_sha = signer.sha256_hex(raw)
        sig = signer.sign(raw)
        ReleaseManifest.objects.filter(package=pkg, is_active=True).update(is_active=False)
        ReleaseManifest.objects.create(
            package=pkg,
            manifest_version=(pkg.manifests.count() + 1),
            document=doc,
            document_sha256=doc_sha,
            signature=sig,
            is_active=True,
        )

        ConfigurationVersion.objects.filter(package=pkg).update(
            is_active=True, published_at=timezone.now()
        )

        UpdateAuditService().write(
            event_code="UPD-PUBLISH",
            message=f"Release published: {pkg.version}",
            correlation_id=correlation_id or uuid.uuid4(),
            department_id=pkg.department_id,
            version=pkg.version,
            details={"package_id": str(pkg.id)},
            severity=SyncLogSeverity.INFO,
        )
        return self.get(pkg.id) or self.serialize(pkg)

    @staticmethod
    def serialize(pkg: ReleasePackage) -> dict[str, Any]:
        return {
            "id": str(pkg.id),
            "package_type": pkg.package_type,
            "channel": pkg.channel,
            "version": pkg.version,
            "display_name": pkg.display_name,
            "description": pkg.description,
            "status": pkg.status,
            "download_url": pkg.download_url,
            "package_size_bytes": pkg.package_size_bytes,
            "sha256": pkg.sha256,
            "signature": pkg.signature,
            "publisher": pkg.publisher,
            "min_agent_version": pkg.min_agent_version,
            "min_schema_version": pkg.min_schema_version,
            "security_version": pkg.security_version,
            "recovery_version": pkg.recovery_version,
            "api_version": pkg.api_version,
            "compatibility": pkg.compatibility or {},
            "dependencies": pkg.dependencies or [],
            "plugin_id": pkg.plugin_id,
            "department_id": str(pkg.department_id) if pkg.department_id else None,
            "published_at": pkg.published_at.isoformat() if pkg.published_at else None,
            "created_at": pkg.created_at.isoformat() if pkg.created_at else None,
            "created_by": pkg.created_by,
        }
