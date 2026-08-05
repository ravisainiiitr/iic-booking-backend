"""Production commissioning — automatic PASS / WARNING / FAIL checks."""

from __future__ import annotations

from typing import Any

from django.db import connection
from django.utils import timezone

from iic_booking.remote_analysis.constants import HEARTBEAT_OFFLINE_SECONDS, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.fleet_inventory import equipment_ra_config_audit, fleet_inventory
from iic_booking.remote_analysis.services.workstation_identity import WorkstationIdentityService
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.tunnel_models import TunnelSession


def _check(name: str, status: str, detail: str, *, recommendation: str = "") -> dict[str, Any]:
    return {
        "name": name,
        "status": status,  # PASS | WARNING | FAIL
        "detail": detail,
        "recommendation": recommendation,
    }


def run_production_commissioning() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    # Database
    try:
        connection.ensure_connection()
        with connection.cursor() as c:
            c.execute("SELECT 1")
            c.fetchone()
        checks.append(_check("Database", "PASS", "SELECT 1 succeeded"))
    except Exception as exc:
        checks.append(_check("Database", "FAIL", str(exc), recommendation="Check DATABASE_URL / Postgres"))

    # Redis / Celery — soft
    try:
        from django.core.cache import cache

        cache.set("ra_commission_ping", "1", 10)
        ok = cache.get("ra_commission_ping") == "1"
        checks.append(
            _check("Cache/Redis", "PASS" if ok else "WARNING", "Cache round-trip " + ("ok" if ok else "failed"))
        )
    except Exception as exc:
        checks.append(_check("Cache/Redis", "WARNING", str(exc), recommendation="Verify Redis for Celery/cache"))

    # Transport / Gateway settings — Reverse Tunnel is mandatory.
    settings_obj = RemoteAnalysisSettings.get_solo()
    mode = getattr(settings_obj, "transport_mode", "") or "reverse_tunnel"
    if mode != "reverse_tunnel":
        checks.append(
            _check(
                "Reverse Tunnel Config",
                "FAIL",
                f"unsupported transport_mode={mode}",
                recommendation="Set transport_mode=reverse_tunnel (sole supported mode)",
            )
        )
    elif (settings_obj.tunnel_gateway_admin_url or "").strip() and (
        settings_obj.tunnel_gateway_wss_url or ""
    ).strip():
        checks.append(_check("Reverse Tunnel Config", "PASS", f"transport_mode={mode}"))
    else:
        checks.append(
            _check(
                "Reverse Tunnel Config",
                "FAIL",
                "reverse_tunnel enabled but gateway URLs empty",
                recommendation="Set tunnel_gateway_admin_url and tunnel_gateway_wss_url",
            )
        )

    # Fleet / heartbeat (enabled PCs only)
    fleet = fleet_inventory()
    enabled_total = AnalysisWorkstation.objects.filter(enabled=True).count()
    online = (
        AnalysisWorkstation.objects.filter(enabled=True)
        .exclude(status=WorkstationStatus.OFFLINE)
        .exclude(last_heartbeat__isnull=True)
        .count()
    )
    # Prefer heartbeat-fresh count from inventory rows
    online = sum(1 for r in fleet["workstations"] if r.get("enabled") and r.get("online"))
    if enabled_total == 0:
        checks.append(_check("Analysis PC Fleet", "FAIL", "No enabled workstations registered"))
    elif online == 0:
        checks.append(
            _check(
                "Heartbeat / Online PCs",
                "FAIL",
                f"0/{enabled_total} enabled online",
                recommendation="Restart Agent service; verify enrollment key and Portal URL",
            )
        )
    elif online < enabled_total:
        checks.append(
            _check(
                "Heartbeat / Online PCs",
                "WARNING",
                f"{online}/{enabled_total} enabled online",
                recommendation="Investigate OFFLINE agents; deploy Agent with machine fingerprint",
            )
        )
    else:
        checks.append(_check("Heartbeat / Online PCs", "PASS", f"{online}/{enabled_total} enabled online"))

    # Duplicates
    dups = WorkstationIdentityService().list_duplicates()
    if dups:
        checks.append(
            _check(
                "Duplicate Workstations",
                "FAIL",
                f"{len(dups)} duplicate group(s)",
                recommendation="POST /api/v1/analysis/fleet/duplicates/merge/ or auto-merge",
            )
        )
    else:
        checks.append(_check("Duplicate Workstations", "PASS", "No duplicate groups"))

    # Orphan tunnels
    orphans = TunnelSession.objects.exclude(status__in=["CLOSED", "FAILED", "EXPIRED"]).count()
    if orphans:
        checks.append(
            _check(
                "Tunnel Orphans",
                "WARNING",
                f"{orphans} non-closed tunnel(s)",
                recommendation="Run SessionCleanup / ops orphan cleanup",
            )
        )
    else:
        checks.append(_check("Tunnel Orphans", "PASS", "No open orphan tunnels"))

    # Equipment config
    audit = equipment_ra_config_audit()
    if audit["total_ra_equipment"] == 0:
        checks.append(_check("RAW/RESULTS Config", "WARNING", "No RA-enabled equipment"))
    elif audit["incomplete"]:
        checks.append(
            _check(
                "RAW/RESULTS Config",
                "FAIL",
                f"{audit['incomplete']}/{audit['total_ra_equipment']} incomplete",
                recommendation="Configure RAW/RESULTS dirs and required software",
            )
        )
    else:
        checks.append(
            _check(
                "RAW/RESULTS Config",
                "PASS",
                f"All {audit['total_ra_equipment']} RA equipment configured",
            )
        )

    # Guacamole mock
    if settings_obj.mock_guacamole:
        checks.append(
            _check(
                "Guacamole",
                "WARNING",
                "mock_guacamole=True",
                recommendation="Disable mock for production desktops",
            )
        )
    else:
        checks.append(_check("Guacamole", "PASS", "mock_guacamole=False"))

    # End Analysis route
    try:
        from django.urls import reverse

        reverse("api:booking-analysis-end", kwargs={"booking_id": 1})
        reverse("api:booking-analysis-start", kwargs={"booking_id": 1})
        checks.append(_check("End/Start Analysis APIs", "PASS", "Routes registered"))
    except Exception as exc:
        checks.append(_check("End/Start Analysis APIs", "FAIL", str(exc)))

    # Maintenance monitor import
    try:
        from iic_booking.remote_analysis.services.maintenance import MaintenanceService
        from iic_booking.remote_analysis.services.checkin import CheckinService

        _ = MaintenanceService().fleet_dashboard()
        _ = CheckinService
        checks.append(_check("Scheduler Extensions", "PASS", "Maintenance + Check-in services load"))
    except Exception as exc:
        checks.append(_check("Scheduler Extensions", "FAIL", str(exc)))

    fails = sum(1 for c in checks if c["status"] == "FAIL")
    warns = sum(1 for c in checks if c["status"] == "WARNING")
    overall = "FAIL" if fails else ("WARNING" if warns else "PASS")

    return {
        "overall": overall,
        "generated_at": timezone.now().isoformat(),
        "summary": {"pass": len(checks) - fails - warns, "warning": warns, "fail": fails},
        "checks": checks,
        "fleet_counts": fleet["counts"],
        "equipment_audit": {
            "total": audit["total_ra_equipment"],
            "incomplete": audit["incomplete"],
            "incomplete_codes": [e["code"] for e in audit["incomplete_equipment"]],
        },
        "duplicates": dups,
    }
