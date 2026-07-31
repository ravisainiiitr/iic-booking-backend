"""
Phase 4 — Live Production Commissioning dashboard (ops-only).

Aggregates existing probes (gateway, agent, tunnels, Guacamole, DSA-adjacent
workspace sync) into GREEN / AMBER / RED status cards. Does not change
product workflows — read-only observability plus optional run binding.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    HEARTBEAT_OFFLINE_SECONDS,
    CommandStatus,
    SessionStatus,
    TransportMode,
    TunnelSessionStatus,
    WorkstationStatus,
    WorkspaceStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.operations.toolkit import (
    probe_guacamole,
    probe_redis,
    probe_reverse_tunnel,
    probe_storage_usage,
)
from iic_booking.remote_analysis.operations_models import CommissioningRun, CommissioningRunStatus
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings, RemoteDesktopSession
from iic_booking.remote_analysis.tunnel_models import TunnelSession
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace


STATUS_GREEN = "GREEN"
STATUS_AMBER = "AMBER"
STATUS_RED = "RED"


def _card(name: str, status: str, detail: str, *, metrics: dict | None = None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "metrics": metrics or {},
    }


def _overall(cards: list[dict[str, Any]]) -> str:
    ranks = {STATUS_GREEN: 0, STATUS_AMBER: 1, STATUS_RED: 2}
    worst = STATUS_GREEN
    for c in cards:
        if ranks.get(c["status"], 0) > ranks[worst]:
            worst = c["status"]
    return worst


def _gateway_card(tunnel_probe: dict[str, Any], settings_obj: RemoteAnalysisSettings) -> dict[str, Any]:
    mode = settings_obj.transport_mode
    if mode == TransportMode.DIRECT_RDP:
        return _card(
            "Gateway Status",
            STATUS_AMBER,
            "transport_mode=direct_rdp (reverse tunnel idle)",
            metrics={"transport_mode": mode},
        )
    st = (tunnel_probe.get("status") or "").upper()
    if st == "PASS":
        return _card("Gateway Status", STATUS_GREEN, tunnel_probe.get("detail") or "ok", metrics=tunnel_probe.get("gateway_health") or {})
    if st in {"WARN", "INFO"}:
        return _card("Gateway Status", STATUS_AMBER, tunnel_probe.get("detail") or st, metrics=tunnel_probe.get("gateway_health") or {})
    return _card("Gateway Status", STATUS_RED, tunnel_probe.get("detail") or "gateway unhealthy", metrics=tunnel_probe.get("gateway_health") or {})


def _agents_card(now) -> dict[str, Any]:
    online_statuses = {
        WorkstationStatus.ONLINE,
        WorkstationStatus.AVAILABLE,
        WorkstationStatus.PREPARING,
        WorkstationStatus.BUSY,
        WorkstationStatus.CLEANING,
    }
    rows = []
    online = 0
    for ws in AnalysisWorkstation.objects.filter(enabled=True):
        age = int((now - ws.last_heartbeat).total_seconds()) if ws.last_heartbeat else None
        is_online = ws.status in online_statuses and age is not None and age <= HEARTBEAT_OFFLINE_SECONDS
        if is_online:
            online += 1
        rows.append(
            {
                "id": str(ws.id),
                "hostname": ws.hostname or ws.display_name,
                "status": ws.status,
                "online": is_online,
                "heartbeat_age_seconds": age,
                "health_score": ws.health_score,
            }
        )
    if not rows:
        return _card("Connected Agents", STATUS_RED, "No enabled workstations", metrics={"agents": []})
    if online == 0:
        return _card("Connected Agents", STATUS_RED, "No agents heartbeating", metrics={"online": 0, "agents": rows})
    if online < len(rows):
        return _card(
            "Connected Agents",
            STATUS_AMBER,
            f"{online}/{len(rows)} online",
            metrics={"online": online, "total": len(rows), "agents": rows},
        )
    return _card(
        "Connected Agents",
        STATUS_GREEN,
        f"{online} online",
        metrics={"online": online, "total": len(rows), "agents": rows},
    )


def _tunnels_card(tunnel_probe: dict[str, Any]) -> dict[str, Any]:
    active = int(tunnel_probe.get("active_tunnels") or 0)
    recent = tunnel_probe.get("recent_tunnels") or []
    metrics_gw = tunnel_probe.get("gateway_metrics") or {}
    if active > 0:
        return _card(
            "Current Reverse Tunnels",
            STATUS_GREEN,
            f"{active} active",
            metrics={
                "active": active,
                "bytes_sent": metrics_gw.get("bytes_sent"),
                "bytes_received": metrics_gw.get("bytes_received"),
                "recent": recent[:10],
            },
        )
    mode_ok = (tunnel_probe.get("transport_mode") or "") == TransportMode.REVERSE_TUNNEL
    if mode_ok and (tunnel_probe.get("status") or "").upper() == "PASS":
        return _card("Current Reverse Tunnels", STATUS_AMBER, "Gateway up; no active tunnels", metrics={"recent": recent[:5]})
    return _card("Current Reverse Tunnels", STATUS_AMBER, "No active tunnels", metrics={"recent": recent[:5]})


def _guacamole_card(guac: dict[str, Any]) -> dict[str, Any]:
    st = (guac.get("status") or "").upper()
    if st == "PASS":
        return _card("Guacamole Session State", STATUS_GREEN, guac.get("detail") or "ok", metrics=guac)
    if "mock" in str(guac.get("detail") or "").lower() or guac.get("mock"):
        return _card("Guacamole Session State", STATUS_AMBER, guac.get("detail") or "mock mode", metrics=guac)
    if st in {"WARN", "INFO"}:
        return _card("Guacamole Session State", STATUS_AMBER, guac.get("detail") or st, metrics=guac)
    return _card("Guacamole Session State", STATUS_RED, guac.get("detail") or "unhealthy", metrics=guac)


def _workspace_sync_card() -> dict[str, Any]:
    active = AnalysisWorkspace.objects.exclude(
        status__in={WorkspaceStatus.DELETED, WorkspaceStatus.ARCHIVED}
    ).order_by("-updated_at")[:15]
    rows = [
        {
            "id": str(w.id),
            "booking_id": w.booking_id,
            "status": w.status,
            "sync_phase": w.sync_phase,
            "sync_message": (w.sync_message or "")[:200],
        }
        for w in active
    ]
    failing = [r for r in rows if "fail" in str(r["status"]).lower() or "error" in str(r.get("sync_phase") or "").lower()]
    if failing:
        return _card("Workspace Sync State", STATUS_RED, f"{len(failing)} workspace(s) in error", metrics={"workspaces": rows})
    if rows:
        return _card("Workspace Sync State", STATUS_GREEN, f"{len(rows)} active workspace(s)", metrics={"workspaces": rows})
    return _card("Workspace Sync State", STATUS_AMBER, "No active workspaces", metrics={"workspaces": []})


def _command_card() -> dict[str, Any]:
    pending = RemoteCommand.objects.filter(
        status__in={CommandStatus.PENDING, CommandStatus.DELIVERED, CommandStatus.RUNNING}
    ).order_by("-created_at")[:20]
    rows = [
        {
            "id": str(c.id),
            "type": c.command_type,
            "status": c.status,
            "workstation": c.workstation.hostname if c.workstation_id else None,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        }
        for c in pending
    ]
    failed_recent = RemoteCommand.objects.filter(
        status=CommandStatus.FAILED,
        created_at__gte=timezone.now() - timedelta(hours=1),
    ).count()
    if failed_recent:
        return _card(
            "Current Command",
            STATUS_AMBER,
            f"{failed_recent} failed in last hour; {len(rows)} in-flight",
            metrics={"in_flight": rows, "failed_last_hour": failed_recent},
        )
    if rows:
        return _card("Current Command", STATUS_GREEN, f"{len(rows)} in-flight", metrics={"in_flight": rows})
    return _card("Current Command", STATUS_GREEN, "Idle", metrics={"in_flight": []})


def _desktop_sessions_card() -> dict[str, Any]:
    open_statuses = {
        SessionStatus.CREATED,
        SessionStatus.PREPARING,
        SessionStatus.READY,
        SessionStatus.TOKEN_GENERATED,
        SessionStatus.LAUNCHED,
        SessionStatus.CONNECTING,
        SessionStatus.CONNECTED,
        SessionStatus.ACTIVE,
        SessionStatus.IDLE,
    }
    sessions = RemoteDesktopSession.objects.filter(status__in=open_statuses).order_by("-created_at")[:20]
    rows = [
        {
            "id": str(s.id),
            "status": s.status,
            "booking_id": s.booking_id,
            "workstation": s.workstation.hostname if s.workstation_id else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
        for s in sessions
    ]
    if rows:
        return _card("Windows / Desktop Session", STATUS_GREEN, f"{len(rows)} open session(s)", metrics={"sessions": rows})
    return _card("Windows / Desktop Session", STATUS_AMBER, "No open desktop sessions", metrics={"sessions": []})


def _rdp_reachability_card(settings_obj: RemoteAnalysisSettings) -> dict[str, Any]:
    """
    Operational hint only — true RDP reachability is via tunnel or direct from guacd.
    """
    if settings_obj.transport_mode == TransportMode.REVERSE_TUNNEL:
        return _card(
            "RDP Reachability",
            STATUS_GREEN,
            "reverse_tunnel: guacd→adapter; agent→localhost:3389",
            metrics={"path": "reverse_tunnel"},
        )
    return _card(
        "RDP Reachability",
        STATUS_AMBER,
        "direct_rdp: guacd must reach workstation:3389 on the lab network",
        metrics={"path": "direct_rdp"},
    )


def _dsa_sync_card() -> dict[str, Any]:
    """DSA itself is out-of-process; surface workspace RawData readiness as proxy."""
    recent = AnalysisWorkspace.objects.filter(updated_at__gte=timezone.now() - timedelta(days=7)).count()
    return _card(
        "DSA Sync Status",
        STATUS_AMBER if recent == 0 else STATUS_GREEN,
        "Workspace RawData readiness is the portal-side proxy; confirm DSA agent logs on Support PC",
        metrics={"workspaces_touched_7d": recent},
    )


def _runs_card() -> dict[str, Any]:
    running = CommissioningRun.objects.filter(status=CommissioningRunStatus.RUNNING).order_by("-started_at")[:10]
    rows = [
        {
            "id": str(r.id),
            "started_at": r.started_at.isoformat() if r.started_at else None,
            "workstation": r.workstation.hostname if r.workstation_id else None,
            "booking_id": r.booking_id,
        }
        for r in running
    ]
    if rows:
        return _card("Live Commissioning Runs", STATUS_GREEN, f"{len(rows)} RUNNING", metrics={"runs": rows})
    return _card("Live Commissioning Runs", STATUS_AMBER, "No RUNNING commissioning runs", metrics={"runs": []})


def build_live_commissioning_dashboard(*, workstation_id: str | None = None) -> dict[str, Any]:
    """Color-coded live commissioning snapshot for Toolkit / Live Commissioning page."""
    now = timezone.now()
    settings_obj = RemoteAnalysisSettings.get_solo()
    tunnel_probe = probe_reverse_tunnel()
    guac = probe_guacamole()
    redis_info = probe_redis()
    storage = probe_storage_usage()

    cards = [
        _gateway_card(tunnel_probe, settings_obj),
        _agents_card(now),
        _tunnels_card(tunnel_probe),
        _card(
            "Tunnel Latency / Bandwidth",
            STATUS_AMBER if not (tunnel_probe.get("gateway_metrics") or {}).get("ok", True) else STATUS_GREEN,
            "From Gateway /metrics when available",
            metrics=tunnel_probe.get("gateway_metrics") or {},
        ),
        _card(
            "Agent Heartbeat",
            _agents_card(now)["status"],
            "Derived from workstation last_heartbeat",
            metrics={"offline_threshold_seconds": HEARTBEAT_OFFLINE_SECONDS},
        ),
        _workspace_sync_card(),
        _command_card(),
        _guacamole_card(guac),
        _dsa_sync_card(),
        _rdp_reachability_card(settings_obj),
        _desktop_sessions_card(),
        _runs_card(),
        _card(
            "Portal Infra (Redis/Storage)",
            STATUS_GREEN if (redis_info.get("status") or "").upper() != "FAIL" else STATUS_RED,
            "Redis + storage probes",
            metrics={"redis": redis_info, "storage": storage},
        ),
    ]

    if workstation_id:
        cards.append(_workstation_focus_card(workstation_id, now))

    return {
        "generated_at": now.isoformat(),
        "overall": _overall(cards),
        "cards": cards,
        "transport_mode": settings_obj.transport_mode,
        "mock_guacamole": bool(settings_obj.mock_guacamole),
        "links": {
            "toolkit": "/api/v1/analysis/operations/toolkit/?view=html",
            "commissioning": "/api/v1/analysis/operations/commissioning/?view=html",
            "live": "/api/v1/analysis/operations/toolkit/live/?view=html",
            "faults": "/api/v1/analysis/operations/toolkit/faults/?view=html",
        },
    }


def _workstation_focus_card(workstation_id: str, now) -> dict[str, Any]:
    ws = AnalysisWorkstation.objects.filter(pk=workstation_id).first()
    if not ws:
        return _card("Allocated Workstation", STATUS_RED, f"Unknown workstation {workstation_id}")
    age = int((now - ws.last_heartbeat).total_seconds()) if ws.last_heartbeat else None
    tunnels = list(
        TunnelSession.objects.filter(workstation=ws)
        .order_by("-created_at")[:5]
        .values("id", "status", "adapter_port", "bytes_sent", "bytes_received", "reconnect_count")
    )
    for t in tunnels:
        t["id"] = str(t["id"])
    status = STATUS_GREEN if age is not None and age <= HEARTBEAT_OFFLINE_SECONDS else STATUS_RED
    return _card(
        "Allocated Workstation",
        status,
        f"{ws.hostname or ws.display_name} · {ws.status}",
        metrics={
            "workstation_id": str(ws.id),
            "heartbeat_age_seconds": age,
            "health_score": ws.health_score,
            "tunnels": tunnels,
        },
    )


def build_live_session_timeline(*, booking_id: int | None = None, run_id: str | None = None) -> dict[str, Any]:
    """
    Unified live timeline from commissioning steps + tunnel/desktop/workspace events.
    """
    from iic_booking.remote_analysis.operations.commissioning_observability import timeline_payload

    events: list[dict[str, Any]] = []
    run = None
    if run_id:
        run = CommissioningRun.objects.filter(pk=run_id).first()
        if run:
            for step in timeline_payload(run).get("steps") or []:
                events.append(
                    {
                        "timestamp": step.get("started_at") or step.get("ended_at"),
                        "event": step.get("name"),
                        "duration_ms": step.get("duration_ms"),
                        "success": step.get("success"),
                        "booking_id": run.booking_id,
                        "workstation_id": str(run.workstation_id) if run.workstation_id else None,
                        "analysis_job_id": None,
                        "tunnel_id": None,
                        "source": "commissioning_run",
                        "meta": step.get("meta") or {},
                    }
                )

    qs_tun = TunnelSession.objects.all().order_by("-created_at")[:50]
    if booking_id:
        qs_tun = TunnelSession.objects.filter(booking_id=booking_id).order_by("-created_at")[:50]
    for t in qs_tun:
        events.append(
            {
                "timestamp": t.created_at.isoformat() if t.created_at else None,
                "event": "TunnelRequested",
                "duration_ms": None,
                "success": True,
                "booking_id": t.booking_id,
                "workstation_id": str(t.workstation_id),
                "analysis_job_id": str(t.analysis_job_id) if t.analysis_job_id else None,
                "tunnel_id": str(t.id),
                "source": "tunnel_session",
                "meta": {"status": t.status, "adapter_port": t.adapter_port},
            }
        )
        if t.agent_joined_at:
            events.append(
                {
                    "timestamp": t.agent_joined_at.isoformat(),
                    "event": "AgentAccepted",
                    "booking_id": t.booking_id,
                    "workstation_id": str(t.workstation_id),
                    "tunnel_id": str(t.id),
                    "source": "tunnel_session",
                    "meta": {},
                }
            )
        if t.activated_at:
            events.append(
                {
                    "timestamp": t.activated_at.isoformat(),
                    "event": "TunnelConnected",
                    "booking_id": t.booking_id,
                    "workstation_id": str(t.workstation_id),
                    "tunnel_id": str(t.id),
                    "source": "tunnel_session",
                    "meta": {"reconnect_count": t.reconnect_count},
                }
            )
        if t.closed_at:
            events.append(
                {
                    "timestamp": t.closed_at.isoformat(),
                    "event": "TunnelDestroyed",
                    "booking_id": t.booking_id,
                    "workstation_id": str(t.workstation_id),
                    "tunnel_id": str(t.id),
                    "source": "tunnel_session",
                    "meta": {"reason": t.close_reason},
                }
            )

    sess_qs = RemoteDesktopSession.objects.all().order_by("-created_at")[:30]
    if booking_id:
        sess_qs = RemoteDesktopSession.objects.filter(booking_id=booking_id).order_by("-created_at")[:30]
    for s in sess_qs:
        events.append(
            {
                "timestamp": s.created_at.isoformat() if s.created_at else None,
                "event": "GuacamoleSessionCreated",
                "booking_id": s.booking_id,
                "workstation_id": str(s.workstation_id) if s.workstation_id else None,
                "tunnel_id": None,
                "source": "desktop_session",
                "meta": {"status": s.status, "session_id": str(s.id)},
            }
        )
        if s.connected_at:
            events.append(
                {
                    "timestamp": s.connected_at.isoformat(),
                    "event": "UserConnected",
                    "booking_id": s.booking_id,
                    "workstation_id": str(s.workstation_id) if s.workstation_id else None,
                    "source": "desktop_session",
                    "meta": {"session_id": str(s.id)},
                }
            )

    events.sort(key=lambda e: e.get("timestamp") or "", reverse=True)
    return {
        "generated_at": timezone.now().isoformat(),
        "booking_id": booking_id,
        "commissioning_run_id": str(run.id) if run else run_id,
        "events": events[:200],
    }
