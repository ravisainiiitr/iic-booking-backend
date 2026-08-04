"""Health detectors that open LabAlert rows (Celery-friendly)."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from iic_booking.lab_infrastructure.models import LabAlert
from iic_booking.remote_analysis.constants import HEARTBEAT_OFFLINE_SECONDS
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.fleet_inventory import fleet_inventory
from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import DepartmentSyncAgent
from iic_booking.sync.services.tokens import agent_expected_versions


def _upsert_alert(*, code: str, fingerprint: str, severity: str, title: str, detail: str, node_kind: str, node_id: str):
    existing = LabAlert.objects.filter(fingerprint=fingerprint, status__in=["open", "acknowledged"]).first()
    if existing:
        existing.detail = detail
        existing.severity = severity
        existing.title = title
        existing.updated_at = timezone.now()
        existing.save(update_fields=["detail", "severity", "title", "updated_at"])
        return existing
    return LabAlert.objects.create(
        code=code,
        fingerprint=fingerprint,
        severity=severity,
        title=title,
        detail=detail,
        node_kind=node_kind,
        node_id=node_id,
        source="lab",
    )


def run_health_detectors() -> dict:
    """Detect offline agents, heartbeat timeouts, config drift, disk full, duplicates."""
    opened = 0
    now = timezone.now()
    dsa_timeout = heartbeat_timeout_seconds()

    for agent in DepartmentSyncAgent.objects.filter(is_active=True).iterator():
        node_id = f"dsa:{agent.pk}"
        age = (now - agent.last_heartbeat_at).total_seconds() if agent.last_heartbeat_at else None
        if age is None or age > dsa_timeout:
            _upsert_alert(
                code="heartbeat_timeout",
                fingerprint=f"hb-timeout-{node_id}",
                severity="critical" if age is None or age > dsa_timeout * 2 else "error",
                title=f"DSA offline: {agent.agent_name}",
                detail=f"Last heartbeat age={age}",
                node_kind="dsa",
                node_id=node_id,
            )
            opened += 1
        try:
            expected_cfg, _ = agent_expected_versions(agent)
            reported = agent.last_reported_configuration_version
            if reported is not None and expected_cfg is not None and int(reported) != int(expected_cfg):
                _upsert_alert(
                    code="configuration_drift",
                    fingerprint=f"cfg-drift-{node_id}",
                    severity="warning",
                    title=f"Config drift: {agent.agent_name}",
                    detail=f"reported={reported} expected={expected_cfg}",
                    node_kind="dsa",
                    node_id=node_id,
                )
                opened += 1
        except Exception:
            pass

        # Equipment PC rollup from latest heartbeat details
        from iic_booking.sync.models import AgentHeartbeat

        hb = AgentHeartbeat.objects.filter(sync_agent=agent).order_by("-reported_at").first()
        if hb and isinstance((hb.details or {}).get("equipment_pcs"), list):
            for pc in hb.details["equipment_pcs"]:
                disk = pc.get("disk_used_percent") or pc.get("diskUsedPercent")
                mac = pc.get("mac_address") or pc.get("macAddress") or "unknown"
                eq_id = f"eqpc:{pc.get('id') or mac}"
                if disk is not None and float(disk) >= 95:
                    _upsert_alert(
                        code="disk_full",
                        fingerprint=f"disk-{eq_id}",
                        severity="critical",
                        title=f"Disk nearly full on {pc.get('hostname') or mac}",
                        detail=f"Disk used {disk}%",
                        node_kind="equipment_pc",
                        node_id=eq_id,
                    )
                    opened += 1
                if pc.get("last_error") or pc.get("lastError"):
                    _upsert_alert(
                        code="equipment_pc_error",
                        fingerprint=f"err-{eq_id}",
                        severity="error",
                        title=f"Equipment PC error: {pc.get('hostname') or mac}",
                        detail=str(pc.get("last_error") or pc.get("lastError")),
                        node_kind="equipment_pc",
                        node_id=eq_id,
                    )
                    opened += 1

    for ws in AnalysisWorkstation.objects.filter(enabled=True).iterator():
        node_id = f"raa:{ws.pk}"
        age = (now - ws.last_heartbeat).total_seconds() if ws.last_heartbeat else None
        if age is None or age > HEARTBEAT_OFFLINE_SECONDS:
            _upsert_alert(
                code="analysis_pc_offline",
                fingerprint=f"hb-timeout-{node_id}",
                severity="critical",
                title=f"Analysis PC offline: {ws.hostname}",
                detail=f"Last heartbeat age={age}",
                node_kind="analysis_pc",
                node_id=node_id,
            )
            opened += 1

    try:
        from iic_booking.remote_analysis.services.workstation_identity import WorkstationIdentityService

        for d in WorkstationIdentityService().list_duplicates() or []:
            fp = f"dup-{d.get('fingerprint') or d.get('key') or d.get('hostname') or id(d)}"
            _upsert_alert(
                code="duplicate_registration",
                fingerprint=str(fp)[:128],
                severity="warning",
                title="Duplicate Analysis PC registration",
                detail=str(d)[:500],
                node_kind="analysis_pc",
                node_id=str(d.get("id") or d.get("hostname") or ""),
            )
            opened += 1
    except Exception:
        pass

    return {"opened_or_updated": opened, "generated_at": now.isoformat()}
