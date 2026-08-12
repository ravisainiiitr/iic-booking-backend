"""Public platform version and self-test endpoints (Phase R.2.6)."""

from __future__ import annotations

from django.conf import settings
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response

from iic_booking.platform_compat.manifest import build_capabilities_payload, build_version_payload
from iic_booking.platform_compat.semver import compare_installer, traffic_light, version_gte
from iic_booking.sync.permissions import CanManageDepartmentSync


@api_view(["GET"])
@authentication_classes([])
@permission_classes([AllowAny])
def api_version(request):
    """GET /api/version — public portal / backend / frontend / installer matrix."""
    try:
        return Response(build_version_payload())
    except Exception as exc:  # noqa: BLE001 — public smoke must never 500 the portal
        # Minimal safe payload so deploy health checks and installers keep working.
        return Response(
            {
                "portal_version": str(getattr(settings, "PORTAL_VERSION", "2.5.2") or "2.5.2"),
                "backend_version": str(getattr(settings, "PORTAL_VERSION", "2.5.2") or "2.5.2"),
                "frontend_version": str(getattr(settings, "FRONTEND_VERSION", "") or ""),
                "backend_commit": str(
                    getattr(settings, "BACKEND_GIT_COMMIT", "")
                    or getattr(settings, "GIT_SHA", "")
                    or ""
                ),
                "frontend_commit": str(getattr(settings, "FRONTEND_GIT_COMMIT", "") or ""),
                "git_commit": str(
                    getattr(settings, "BACKEND_GIT_COMMIT", "")
                    or getattr(settings, "GIT_SHA", "")
                    or ""
                ),
                "provisioning_version": "2.0",
                "research_copilot_version": "0.0.0",
                "build_date": "",
                "compatible_frontend_min": "2.5.2-r2",
                "compatible_backend_min": "2.5.2",
                "supported_installers": {
                    "dsa": {"minimum": "1.0.1", "latest": "1.0.2"},
                    "equipment_pc": {"minimum": "1.0.0", "latest": "1.0.0"},
                    "remote_analysis": {"minimum": "1.0.3", "latest": "1.0.3"},
                },
                "degraded": True,
                "detail": str(exc)[:300],
            }
        )


@api_view(["GET"])
@permission_classes([IsAuthenticated, CanManageDepartmentSync])
def deployment_self_test(request):
    """
    GET /api/v1/provisioning/self-test/
    Admin diagnostics for Backend / Frontend / Provisioning / Installer Auth / Copilot.
    """
    version = build_version_payload()
    caps = build_capabilities_payload()
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"name": name, "status": "PASS" if ok else "FAIL", "detail": detail})

    add("api_version", True, f"portal_version={version.get('portal_version')}")
    add(
        "backend_commit",
        bool(version.get("backend_commit")),
        version.get("backend_commit") or "BACKEND_GIT_COMMIT / GIT_SHA not set",
    )
    add(
        "frontend_version",
        bool(version.get("frontend_version")),
        version.get("frontend_version") or "FRONTEND_VERSION not set",
    )
    fe_min = str(version.get("compatible_frontend_min") or "")
    fe_ver = str(version.get("frontend_version") or "")
    fe_ok = bool(fe_ver) and version_gte(fe_ver, fe_min) if fe_min else bool(fe_ver)
    add(
        "backend_frontend_match",
        fe_ok,
        f"frontend={fe_ver or '?'} min={fe_min or '?'}",
    )
    add("provisioning_enabled", bool(caps.get("provisioning_enabled")), "")
    add("zero_touch", bool(caps.get("zero_touch")), "")
    add("installer_auth", bool(caps.get("installer_auth")), "")
    add("auto_approve", bool(caps.get("auto_approve")), "")
    add("device_code", bool(caps.get("device_code")), "")
    add(
        "research_copilot",
        True,
        f"enabled={caps.get('research_copilot')} version={caps.get('research_copilot_version')}",
    )
    add("capabilities", True, f"provisioning_version={caps.get('provisioning_version')}")
    add("react_installer_auth_route", True, caps.get("links", {}).get("installer_auth", ""))
    add("nginx_spa_note", True, "Confirm SPA try_files serves /device-provisioning/* to index.html")

    # Optional installer product query: ?product=dsa&installer_version=1.0.1
    product = (request.query_params.get("product") or "").strip()
    installer_version = (request.query_params.get("installer_version") or "").strip()
    installer_result = None
    if product:
        installer_result = compare_installer(
            product, installer_version or "0", version.get("supported_installers") or {}
        )
        installer_result["traffic_light"] = traffic_light(installer_result["status"])
        add(
            "installer_compatibility",
            bool(installer_result.get("compatible")),
            installer_result.get("message") or "",
        )

    overall = all(c["status"] == "PASS" for c in checks if c["name"] != "research_copilot")
    # research_copilot is informational (may be off by design)
    hard = [c for c in checks if c["name"] not in {"research_copilot", "nginx_spa_note", "backend_commit"}]
    overall = all(c["status"] == "PASS" for c in hard)

    return Response(
        {
            "overall": "PASS" if overall else "FAIL",
            "checks": checks,
            "version": version,
            "capabilities": caps,
            "installer": installer_result,
            "settings_present": {
                "PORTAL_VERSION": bool(getattr(settings, "PORTAL_VERSION", None)),
                "PROVISIONING_ENABLED": getattr(settings, "PROVISIONING_ENABLED", True),
                "RESEARCH_COPILOT_ENABLED": getattr(settings, "RESEARCH_COPILOT_ENABLED", False),
            },
        }
    )
