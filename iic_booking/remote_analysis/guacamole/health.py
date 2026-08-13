"""Session / Guacamole health checks."""

from __future__ import annotations

from django.utils import timezone

from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionHealth


def workstation_agent_online(workstation) -> bool:
    """Soft-online: fresh heartbeat OR allocatable status + valid agent token."""
    return AvailabilityEngine().agent_online(workstation)


def workstation_healthy_for_session(workstation) -> bool:
    """
    Session prepare/Guacamole require a live agent that can pull commands.
    Soft-online (token without heartbeat) is enough for allocation holds, but not
    for cold PREPARE_WORKSTATION.

    During BUSY/PREPARING/RESERVED, older agents may briefly stall heartbeats while
    handling prepare; allow launch if a usable agent token still proves the service
    is enrolled (agent_online), so Guacamole is not blocked after InputReady.
    """
    if not workstation or not workstation.enabled:
        return False
    engine = AvailabilityEngine()
    if engine.heartbeat_fresh(workstation):
        return True
    from iic_booking.remote_analysis.constants import WorkstationStatus

    if workstation.status in {
        WorkstationStatus.BUSY,
        WorkstationStatus.PREPARING,
        WorkstationStatus.RESERVED,
        WorkstationStatus.CLEANING,
    }:
        return engine.agent_online(workstation)
    return False


def refresh_session_health(session: RemoteDesktopSession) -> SessionHealth:
    client = GuacamoleClient()
    guac_ok = client.health_check()
    agent_ok = workstation_agent_online(session.workstation)
    ws_ok = workstation_healthy_for_session(session.workstation)
    score = 100
    details = []
    if not guac_ok:
        score -= 40
        details.append("guacamole_unreachable")
    if not agent_ok:
        score -= 40
        details.append("agent_offline")
    if not ws_ok:
        score -= 20
        details.append("workstation_unhealthy")

    health, _ = SessionHealth.objects.update_or_create(
        session=session,
        defaults={
            "guacamole_reachable": guac_ok,
            "agent_online": agent_ok,
            "workstation_healthy": ws_ok,
            "last_check_at": timezone.now(),
            "detail": ",".join(details),
            "score": max(0, score),
        },
    )
    return health
