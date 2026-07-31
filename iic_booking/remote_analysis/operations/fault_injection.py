"""
Phase 4 — Admin-only fault injection for commissioning recovery validation.

Injects controlled operational faults via existing command / session / tunnel
APIs. Does not redesign product flows. Every injection is audited on a
CommissioningRun when provided.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.constants import CommandType, TunnelSessionStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.operations.commissioning_observability import (
    mark_step,
    start_commissioning_run,
)
from iic_booking.remote_analysis.operations_models import CommissioningRun
from iic_booking.remote_analysis.tunnel_models import TunnelEvent, TunnelSession

logger = logging.getLogger("remote_analysis.fault_injection")

STEP_FAULT_INJECTED = "FaultInjected"

FAULT_CATALOG = [
    {
        "id": "agent_restart",
        "label": "Agent Restart",
        "description": "Enqueue RESTART_AGENT for the selected workstation.",
        "requires": ["workstation_id"],
    },
    {
        "id": "heartbeat_loss",
        "label": "Heartbeat Loss (simulate)",
        "description": "Backdate last_heartbeat so the portal marks the agent stale (does not stop the service).",
        "requires": ["workstation_id"],
    },
    {
        "id": "tunnel_expire",
        "label": "Tunnel Expiry",
        "description": "Mark the newest open tunnel EXPIRED and record a TunnelEvent.",
        "requires": ["workstation_id"],
    },
    {
        "id": "tunnel_close",
        "label": "Tunnel Close",
        "description": "Request CLOSE_TUNNEL for the newest active tunnel (agent-side cleanup).",
        "requires": ["workstation_id"],
    },
    {
        "id": "websocket_disconnect_hint",
        "label": "WebSocket Disconnect (hint)",
        "description": "Record expected recovery steps; actual WS drop must be done on Gateway/Agent host.",
        "requires": [],
    },
    {
        "id": "gateway_restart_hint",
        "label": "Gateway Restart (hint)",
        "description": "Records commissioning step only — restart the reverse-tunnel-gateway container/service on the host.",
        "requires": [],
    },
    {
        "id": "guacamole_restart_hint",
        "label": "Guacamole Restart (hint)",
        "description": "Records commissioning step only — restart guacd/guacamole on the host.",
        "requires": [],
    },
    {
        "id": "workspace_sync_delay",
        "label": "Workspace Sync Delay (simulate)",
        "description": "Set sync_message on the newest active workspace to mark an injected delay for operators.",
        "requires": [],
    },
    {
        "id": "booking_expiry_hint",
        "label": "Booking Expiry (hint)",
        "description": "Does not mutate bookings; documents how to verify access-window expiry in live runs.",
        "requires": ["booking_id"],
    },
]


def list_faults() -> list[dict[str, Any]]:
    return list(FAULT_CATALOG)


def _ensure_run(*, actor, workstation_id: str | None, run_id: str | None, notes: str) -> CommissioningRun:
    if run_id:
        run = CommissioningRun.objects.filter(pk=run_id).first()
        if run:
            return run
    return start_commissioning_run(actor=actor, workstation_id=workstation_id, notes=notes or "fault injection")


def inject_fault(
    *,
    fault_id: str,
    actor=None,
    workstation_id: str | None = None,
    booking_id: int | None = None,
    run_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    fault = next((f for f in FAULT_CATALOG if f["id"] == fault_id), None)
    if not fault:
        return {"ok": False, "error": f"Unknown fault_id={fault_id}", "catalog": list_faults()}

    missing = [r for r in fault["requires"] if r == "workstation_id" and not workstation_id]
    if "booking_id" in fault["requires"] and booking_id is None:
        missing.append("booking_id")
    if missing:
        return {"ok": False, "error": f"Missing required: {', '.join(missing)}", "fault": fault}

    if dry_run:
        return {"ok": True, "dry_run": True, "fault": fault, "would_inject": True}

    run = _ensure_run(
        actor=actor,
        workstation_id=workstation_id,
        run_id=run_id,
        notes=f"fault:{fault_id}",
    )
    result: dict[str, Any] = {"ok": True, "fault_id": fault_id, "commissioning_run_id": str(run.id)}

    if fault_id == "agent_restart":
        ws = AnalysisWorkstation.objects.get(pk=workstation_id)
        cmd = RemoteCommand.objects.create(
            workstation=ws,
            command_type=CommandType.RESTART_AGENT,
            payload={"source": "fault_injection", "commissioning_run_id": str(run.id)},
            created_by=actor if getattr(actor, "pk", None) else None,
        )
        result["command_id"] = str(cmd.id)
        result["detail"] = "RESTART_AGENT enqueued"

    elif fault_id == "heartbeat_loss":
        ws = AnalysisWorkstation.objects.get(pk=workstation_id)
        ws.last_heartbeat = timezone.now() - timedelta(minutes=10)
        ws.save(update_fields=["last_heartbeat", "updated_at"])
        result["detail"] = "last_heartbeat backdated by 10 minutes"

    elif fault_id == "tunnel_expire":
        tunnel = (
            TunnelSession.objects.filter(workstation_id=workstation_id)
            .exclude(status__in={TunnelSessionStatus.CLOSED, TunnelSessionStatus.FAILED, TunnelSessionStatus.EXPIRED})
            .order_by("-created_at")
            .first()
        )
        if not tunnel:
            return {**result, "ok": False, "error": "No open tunnel for workstation"}
        tunnel.status = TunnelSessionStatus.EXPIRED
        tunnel.closed_at = timezone.now()
        tunnel.close_reason = "fault_injection:tunnel_expire"
        tunnel.save(update_fields=["status", "closed_at", "close_reason", "updated_at"])
        TunnelEvent.objects.create(
            tunnel=tunnel,
            event_type="FAULT_INJECT_EXPIRE",
            detail="Commissioning fault injection",
            metadata={"commissioning_run_id": str(run.id)},
        )
        result["tunnel_id"] = str(tunnel.id)
        result["detail"] = "Tunnel marked EXPIRED"

    elif fault_id == "tunnel_close":
        from iic_booking.remote_analysis.tunnel import TunnelOrchestrator

        tunnel = (
            TunnelSession.objects.filter(workstation_id=workstation_id)
            .exclude(status__in={TunnelSessionStatus.CLOSED, TunnelSessionStatus.FAILED, TunnelSessionStatus.EXPIRED})
            .order_by("-created_at")
            .first()
        )
        if not tunnel:
            return {**result, "ok": False, "error": "No open tunnel for workstation"}
        TunnelOrchestrator().close(tunnel, reason="fault_injection:tunnel_close", actor=actor)
        result["tunnel_id"] = str(tunnel.id)
        result["detail"] = "TunnelOrchestrator.close invoked"

    elif fault_id == "workspace_sync_delay":
        from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

        ws_obj = AnalysisWorkspace.objects.order_by("-updated_at").first()
        if not ws_obj:
            return {**result, "ok": False, "error": "No workspace found"}
        prev = ws_obj.sync_message or ""
        ws_obj.sync_message = f"[FAULT_INJECT delay] {prev}"[:500]
        ws_obj.save(update_fields=["sync_message", "updated_at"])
        result["workspace_id"] = str(ws_obj.id)
        result["detail"] = "sync_message annotated for delay simulation"

    elif fault_id in {
        "websocket_disconnect_hint",
        "gateway_restart_hint",
        "guacamole_restart_hint",
        "booking_expiry_hint",
    }:
        result["detail"] = fault["description"]
        result["operator_action_required"] = True
        if booking_id is not None:
            result["booking_id"] = booking_id

    else:
        return {"ok": False, "error": f"Unhandled fault_id={fault_id}"}

    mark_step(
        run,
        STEP_FAULT_INJECTED,
        success=bool(result.get("ok")),
        meta={"fault_id": fault_id, "result": {k: v for k, v in result.items() if k != "ok"}},
    )
    logger.warning(
        "FaultInjected fault_id=%s run=%s actor=%s detail=%s",
        fault_id,
        run.id,
        getattr(actor, "pk", None),
        result.get("detail"),
    )
    return result


def recovery_checklist() -> dict[str, Any]:
    """Static recovery validation checklist for commissioning operators."""
    return {
        "checks": [
            {"id": "agent_reconnect", "label": "Agent reconnect after restart / heartbeat loss"},
            {"id": "gateway_reconnect", "label": "Gateway health returns PASS after restart"},
            {"id": "tunnel_recreation", "label": "New tunnel allocated; no duplicate ACTIVE tunnels"},
            {"id": "workspace_preserved", "label": "Workspace id and files unchanged across reconnect"},
            {"id": "analysis_job_resumed", "label": "Analysis job not duplicated"},
            {"id": "booking_preserved", "label": "Booking access window unchanged"},
            {"id": "no_duplicate_uploads", "label": "No duplicate Processed uploads"},
            {"id": "no_duplicate_workspaces", "label": "One workspace per booking/session"},
            {"id": "no_duplicate_tunnels", "label": "Prior tunnels CLOSED/EXPIRED"},
            {"id": "no_orphan_reservations", "label": "Workstation released for next researcher"},
        ],
    }
