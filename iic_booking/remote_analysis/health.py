"""Liveness / readiness probes for Remote Analysis (Portal)."""

from __future__ import annotations

from django.db import connection
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response


def _liveness_payload() -> dict:
    return {
        "status": "ok",
        "service": "remote_analysis",
        "probe": "liveness",
        "timestamp": timezone.now().isoformat(),
    }


def _readiness_payload() -> tuple[dict, int]:
    checks: dict[str, str] = {}
    ok = True
    try:
        connection.ensure_connection()
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001 — probe must not raise
        checks["database"] = f"error:{type(exc).__name__}"
        ok = False

    try:
        from django.core.cache import cache

        cache.set("ra_ready_probe", "1", timeout=5)
        if cache.get("ra_ready_probe") == "1":
            checks["cache"] = "ok"
        else:
            checks["cache"] = "degraded"
    except Exception as exc:  # noqa: BLE001
        checks["cache"] = f"error:{type(exc).__name__}"

    # Guacamole readiness (production): fail when mock is off but gateway is unreachable.
    # When DEBUG is False, mock Guacamole is never "ready" (pilot/production gate).
    try:
        import os

        from django.conf import settings as django_settings

        from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
        from iic_booking.remote_analysis.guacamole.settings_env import production_guacamole_configured
        from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

        settings_obj = RemoteAnalysisSettings.get_solo()
        configured, problems = production_guacamole_configured(settings_obj)
        if settings_obj.mock_guacamole:
            checks["guacamole"] = "mock"
            if not django_settings.DEBUG:
                checks["guacamole"] = "mock_forbidden_when_debug_false"
                ok = False
        elif not configured:
            checks["guacamole"] = "misconfigured:" + ",".join(problems)
            ok = False
        elif GuacamoleClient(settings_obj).health_check():
            checks["guacamole"] = "ok"
        else:
            checks["guacamole"] = "unreachable"
            ok = False

        enrollment = (os.environ.get("RA_AGENT_ENROLLMENT_KEY") or "").strip()
        if enrollment:
            checks["agent_enrollment"] = "configured"
        elif not django_settings.DEBUG:
            checks["agent_enrollment"] = "missing_RA_AGENT_ENROLLMENT_KEY"
            ok = False
        else:
            checks["agent_enrollment"] = "open_debug"
    except Exception as exc:  # noqa: BLE001
        checks["guacamole"] = f"error:{type(exc).__name__}"

    body = {
        "status": "ready" if ok else "not_ready",
        "service": "remote_analysis",
        "probe": "readiness",
        "checks": checks,
        "timestamp": timezone.now().isoformat(),
    }
    return body, (200 if ok else 503)


@api_view(["GET"])
@permission_classes([AllowAny])
def liveness(request):
    """Process is up — no dependency checks."""
    return Response(_liveness_payload())


@api_view(["GET"])
@permission_classes([AllowAny])
def readiness(request):
    """Ready to serve — validates database connectivity (and optional Redis)."""
    body, status_code = _readiness_payload()
    return Response(body, status=status_code)


@api_view(["GET"])
@permission_classes([AllowAny])
def health(request):
    """Combined health summary for load balancers."""
    live = _liveness_payload()
    ready_body, ready_status = _readiness_payload()
    return Response(
        {
            "status": "healthy" if ready_status == 200 else "unhealthy",
            "liveness": live,
            "readiness": ready_body,
            "version": "1.0.0-rc1",
            "milestones": "1-7 feature complete; 8 production hardening",
        },
        status=ready_status,
    )
