"""Session / Guacamole health checks."""

from __future__ import annotations

from django.utils import timezone

from iic_booking.remote_analysis.constants import HEARTBEAT_OFFLINE_SECONDS, WorkstationStatus
from iic_booking.remote_analysis.guacamole.client import GuacamoleClient
from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionHealth


def workstation_agent_online(workstation) -> bool:
    if not workstation.last_heartbeat:
        return False
    age = (timezone.now() - workstation.last_heartbeat).total_seconds()
    return age <= HEARTBEAT_OFFLINE_SECONDS


def workstation_healthy_for_session(workstation) -> bool:
    if not workstation.enabled:
        return False
    if workstation.status in {
        WorkstationStatus.DISABLED,
        WorkstationStatus.MAINTENANCE,
        WorkstationStatus.ERROR,
        WorkstationStatus.OFFLINE,
    }:
        return False
    return workstation_agent_online(workstation)


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
