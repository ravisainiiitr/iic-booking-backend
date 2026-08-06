"""Portal / installer compatibility manifest (Phase R.2.6)."""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.utils import timezone

# Protocol version for zero-touch device provisioning APIs (not the portal semver).
PROVISIONING_API_VERSION = "2.0"

# Default supported installer matrix — overridable via settings / env JSON.
DEFAULT_SUPPORTED_INSTALLERS: dict[str, dict[str, str]] = {
    "dsa": {"minimum": "1.0.1", "latest": "1.0.2"},
    "equipment_pc": {"minimum": "1.0.0", "latest": "1.0.0"},
    "remote_analysis": {"minimum": "1.0.3", "latest": "1.0.3"},
}


def _setting(name: str, default: Any = "") -> Any:
    return getattr(settings, name, default)


def supported_installers() -> dict[str, dict[str, str]]:
    override = _setting("SUPPORTED_INSTALLERS", None)
    if isinstance(override, dict) and override:
        # Shallow merge so partial env overrides still work.
        merged = {k: dict(v) for k, v in DEFAULT_SUPPORTED_INSTALLERS.items()}
        for key, value in override.items():
            if isinstance(value, dict):
                merged[key] = {**merged.get(key, {}), **{str(k): str(v) for k, v in value.items()}}
        return merged
    return {k: dict(v) for k, v in DEFAULT_SUPPORTED_INSTALLERS.items()}


def build_version_payload() -> dict[str, Any]:
    """Public GET /api/version payload."""
    portal = str(_setting("PORTAL_VERSION", "2.5.2") or "2.5.2")
    backend_commit = str(_setting("BACKEND_GIT_COMMIT", "") or _setting("GIT_SHA", "") or "")
    frontend_commit = str(_setting("FRONTEND_GIT_COMMIT", "") or "")
    frontend_version = str(_setting("FRONTEND_VERSION", "") or "")
    build_date = str(_setting("BUILD_DATE", "") or "")
    if not build_date:
        build_date = timezone.now().strftime("%Y-%m-%d")

    return {
        "portal_version": portal,
        "backend_version": portal,
        "frontend_version": frontend_version or portal,
        "backend_commit": backend_commit,
        "frontend_commit": frontend_commit,
        "git_commit": backend_commit,
        "provisioning_version": str(
            _setting("PROVISIONING_VERSION", PROVISIONING_API_VERSION) or PROVISIONING_API_VERSION
        ),
        "research_copilot_version": str(_setting("RESEARCH_COPILOT_VERSION", "0.0.0") or "0.0.0"),
        "build_date": build_date,
        "compatible_frontend_min": str(_setting("COMPATIBLE_FRONTEND_MIN", "2.5.2-r2") or "2.5.2-r2"),
        "compatible_backend_min": str(_setting("COMPATIBLE_BACKEND_MIN", "2.5.2") or "2.5.2"),
        "supported_installers": supported_installers(),
    }


def build_capabilities_payload() -> dict[str, Any]:
    """Public GET /api/v1/provisioning/capabilities payload."""
    version = build_version_payload()
    provisioning_enabled = bool(_setting("PROVISIONING_ENABLED", True))
    research_enabled = bool(_setting("RESEARCH_COPILOT_ENABLED", False))

    return {
        "zero_touch": provisioning_enabled,
        "installer_auth": provisioning_enabled,
        "provisioning_enabled": provisioning_enabled,
        "provisioning_version": version["provisioning_version"],
        "auto_approve": True,
        "trusted_auto_approve": True,
        "device_code": True,
        "portal_build": version["portal_version"],
        "portal_version": version["portal_version"],
        "backend_commit": version["backend_commit"],
        "frontend_version": version["frontend_version"],
        "frontend_commit": version["frontend_commit"],
        "research_copilot": research_enabled,
        "research_copilot_version": version["research_copilot_version"],
        "supported_installers": version["supported_installers"],
        "links": {
            "installer_auth": "/device-provisioning/installer-auth",
            "console": "/device-provisioning",
            "pending": "/device-provisioning/pending",
            "devices": "/device-provisioning/devices",
            "version": "/api/version",
        },
    }
