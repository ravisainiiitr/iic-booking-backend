"""Package signing and integrity helpers (Milestone 16). Reuses HMAC patterns from M12."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)


class PackageSigningService:
    """SHA-256 integrity + HMAC-SHA256 package/manifest signatures."""

    def signing_key(self) -> bytes:
        raw = getattr(settings, "DSA_RELEASE_SIGNING_KEY", None) or getattr(
            settings, "SECRET_KEY", "dsa-release-dev-key"
        )
        return str(raw).encode("utf-8")

    def sha256_hex(self, content: bytes | str) -> str:
        if isinstance(content, str):
            content = content.encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    def sign(self, payload: str | bytes) -> str:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        digest = hmac.new(self.signing_key(), payload, hashlib.sha256).hexdigest()
        return digest

    def verify(self, payload: str | bytes, signature: str) -> bool:
        if not signature:
            return False
        expected = self.sign(payload)
        return hmac.compare_digest(expected, signature.strip().lower())

    def sign_package_fields(self, *, version: str, sha256: str, download_url: str) -> str:
        canonical = f"{version}|{sha256}|{download_url}"
        return self.sign(canonical)

    def verify_package_fields(
        self, *, version: str, sha256: str, download_url: str, signature: str
    ) -> bool:
        canonical = f"{version}|{sha256}|{download_url}"
        ok = self.verify(canonical, signature)
        if not ok:
            logger.warning("package_signing.invalid version=%s sha256=%s", version, sha256[:12])
        return ok

    def build_manifest_document(self, package, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        doc = {
            "package_id": str(package.id),
            "package_type": package.package_type,
            "channel": package.channel,
            "version": package.version,
            "download_url": package.download_url,
            "sha256": package.sha256,
            "signature": package.signature,
            "publisher": package.publisher,
            "min_agent_version": package.min_agent_version,
            "min_schema_version": package.min_schema_version,
            "security_version": package.security_version,
            "recovery_version": package.recovery_version,
            "api_version": package.api_version,
            "compatibility": package.compatibility or {},
            "dependencies": package.dependencies or [],
            "plugin_id": package.plugin_id or "",
            "package_size_bytes": package.package_size_bytes,
        }
        if extra:
            doc.update(extra)
        return doc
