"""Aggregate Laboratory Infrastructure fleet tree from DSA + RA sources."""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from iic_booking.lab_infrastructure.models import LabNodeKind, LabNodeStatus
from iic_booking.remote_analysis.constants import HEARTBEAT_OFFLINE_SECONDS, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationHeartbeat
from iic_booking.remote_analysis.services.fleet_inventory import fleet_inventory
from iic_booking.sync.admin.constants import heartbeat_timeout_seconds
from iic_booking.sync.models import AgentHeartbeat, DepartmentSyncAgent, EquipmentSyncProfile


def _age_seconds(ts) -> int | None:
    if not ts:
        return None
    return int((timezone.now() - ts).total_seconds())


def _dsa_status(agent: DepartmentSyncAgent, hb: AgentHeartbeat | None) -> str:
    status = (agent.status or "").upper()
    if status in {"MAINTENANCE", "DRAINING"}:
        return LabNodeStatus.MAINTENANCE
    if status in {"DISABLED", "REVOKED", "RETIRED", "DELETED"}:
        return LabNodeStatus.OFFLINE
    timeout = heartbeat_timeout_seconds()
    age = _age_seconds(agent.last_heartbeat_at)
    if age is None or age > timeout:
        return LabNodeStatus.OFFLINE
    if agent.bootstrap_required:
        return LabNodeStatus.SYNCHRONIZING
    if hb and (hb.status_message or "").lower().find("error") >= 0:
        return LabNodeStatus.ERROR
    return LabNodeStatus.ONLINE


def _ra_status(row: dict[str, Any]) -> str:
    if not row.get("online"):
        return LabNodeStatus.OFFLINE
    st = (row.get("status") or "").upper()
    if st == WorkstationStatus.BUSY or row.get("rdp_status") == "SESSION_ACTIVE":
        return LabNodeStatus.BUSY
    if st == WorkstationStatus.MAINTENANCE or st == "MAINTENANCE":
        return LabNodeStatus.MAINTENANCE
    if st in {WorkstationStatus.ERROR, "ERROR"}:
        return LabNodeStatus.ERROR
    if st in {WorkstationStatus.PREPARING, "PREPARING"}:
        return LabNodeStatus.WAITING
    return LabNodeStatus.ONLINE


def _eqpc_status(pc: dict[str, Any], *, parent_online: bool) -> str:
    if not parent_online:
        return LabNodeStatus.OFFLINE
    last = pc.get("last_status_at") or pc.get("last_seen") or pc.get("lastSeen")
    if last:
        try:
            from django.utils.dateparse import parse_datetime

            ts = parse_datetime(str(last).replace("Z", "+00:00")) if isinstance(last, str) else last
            age = _age_seconds(ts)
            if age is not None and age > 300:
                return LabNodeStatus.OFFLINE
        except Exception:
            pass
    if pc.get("last_error") or pc.get("lastError"):
        return LabNodeStatus.ERROR
    sync = (pc.get("sync_status") or pc.get("syncStatus") or "").lower()
    if sync in {"syncing", "synchronizing"}:
        return LabNodeStatus.SYNCHRONIZING
    return LabNodeStatus.ONLINE


def _latest_equipment_pcs(agent: DepartmentSyncAgent) -> list[dict]:
    hb = (
        AgentHeartbeat.objects.filter(sync_agent=agent)
        .order_by("-reported_at")
        .only("details", "reported_at", "cpu_percent", "memory_percent", "disk_percent", "status_message", "service_version", "hostname", "windows_build")
        .first()
    )
    if not hb:
        return [], None
    pcs = (hb.details or {}).get("equipment_pcs") or []
    if not isinstance(pcs, list):
        pcs = []
    return pcs, hb


def build_infrastructure_tree(*, department_id: int | None = None, page: int = 1, page_size: int = 500) -> dict[str, Any]:
    """Department → equipment → DSA / Equipment PCs / Analysis PCs."""
    page = max(1, int(page or 1))
    page_size = min(1000, max(50, int(page_size or 500)))
    agents = DepartmentSyncAgent.objects.select_related("department", "equipment").order_by(
        "department__name", "agent_name"
    )
    if department_id is not None:
        agents = agents.filter(department_id=department_id)

    profiles = EquipmentSyncProfile.objects.select_related(
        "equipment", "equipment__internal_department", "primary_agent"
    )
    if department_id is not None:
        profiles = profiles.filter(equipment__internal_department_id=department_id)

    ra = fleet_inventory(department_id=department_id)
    workstations = ra.get("workstations") or []

    departments: dict[str, dict] = {}

    def dept_bucket(dept) -> dict:
        if dept is None:
            key = "_unassigned"
            name = "Unassigned"
            did = None
        else:
            key = str(dept.pk)
            name = getattr(dept, "name", None) or str(dept)
            did = dept.pk
        if key not in departments:
            departments[key] = {
                "id": did,
                "name": name,
                "nodes": [],
                "equipment": {},
            }
        return departments[key]

    # DSA + Equipment PCs
    for agent in agents:
        bucket = dept_bucket(agent.department)
        pcs, hb = _latest_equipment_pcs(agent)
        dsa_st = _dsa_status(agent, hb)
        parent_online = dsa_st == LabNodeStatus.ONLINE or dsa_st == LabNodeStatus.SYNCHRONIZING
        dsa_node = {
            "id": f"dsa:{agent.pk}",
            "kind": LabNodeKind.DSA,
            "status": dsa_st,
            "computer_name": agent.machine_name or agent.agent_name,
            "equipment": getattr(agent.equipment, "code", None) if agent.equipment_id else None,
            "equipment_name": getattr(agent.equipment, "name", None) if agent.equipment_id else None,
            "department": bucket["name"],
            "department_id": bucket["id"],
            "ip_address": None,
            "mac_address": None,
            "windows_version": (hb.windows_build if hb else None) or agent.operating_system,
            "agent_version": agent.version or (hb.service_version if hb else ""),
            "configuration_version": agent.last_reported_configuration_version,
            "software_version": agent.version,
            "health_score": None,
            "last_heartbeat": agent.last_heartbeat_at.isoformat() if agent.last_heartbeat_at else None,
            "last_synchronization": None,
            "last_configuration_update": None,
            "cpu": hb.cpu_percent if hb else None,
            "memory": hb.memory_percent if hb else None,
            "disk": hb.disk_percent if hb else None,
            "children": [],
        }
        for pc in pcs:
            dsa_node["children"].append(
                {
                    "id": f"eqpc:{pc.get('id') or pc.get('equipment_pc_id') or pc.get('mac_address') or pc.get('macAddress')}",
                    "kind": LabNodeKind.EQUIPMENT_PC,
                    "status": _eqpc_status(pc, parent_online=parent_online),
                    "computer_name": pc.get("hostname") or pc.get("computer_name"),
                    "equipment": pc.get("equipment_id") or pc.get("equipmentId"),
                    "department": bucket["name"],
                    "department_id": bucket["id"],
                    "ip_address": pc.get("observed_ip") or pc.get("observedIp") or pc.get("preferred_ip"),
                    "mac_address": pc.get("mac_address") or pc.get("macAddress"),
                    "windows_version": pc.get("windows_version") or pc.get("windowsVersion"),
                    "agent_version": pc.get("agent_version") or pc.get("agentVersion"),
                    "configuration_version": pc.get("configuration_version") or pc.get("configurationVersion"),
                    "cpu": pc.get("cpu_percent") or pc.get("cpuPercent"),
                    "memory": pc.get("memory_percent") or pc.get("memoryPercent"),
                    "disk": pc.get("disk_used_percent") or pc.get("diskUsedPercent"),
                    "last_heartbeat": pc.get("last_status_at") or pc.get("last_seen"),
                    "parent_dsa_id": str(agent.pk),
                    "raw": pc,
                }
            )
        bucket["nodes"].append(dsa_node)

    # Analysis PCs / RAA
    for row in workstations:
        # Resolve department name from workstation if possible
        ws_id = row.get("id")
        dept_name = "Remote Analysis"
        dept_key = "_ra"
        if dept_key not in departments:
            departments[dept_key] = {
                "id": None,
                "name": "Remote Analysis / Analysis PCs",
                "nodes": [],
                "equipment": {},
            }
        bucket = departments[dept_key]
        st = _ra_status(row)
        bucket["nodes"].append(
            {
                "id": f"raa:{ws_id}",
                "kind": LabNodeKind.ANALYSIS_PC,
                "status": st,
                "computer_name": row.get("hostname") or row.get("display_name"),
                "equipment": None,
                "department": dept_name,
                "department_id": None,
                "ip_address": None,
                "mac_address": None,
                "windows_version": row.get("operating_system"),
                "agent_version": row.get("agent_version"),
                "configuration_version": None,
                "software_version": row.get("agent_version"),
                "health_score": row.get("health_score"),
                "last_heartbeat": row.get("last_heartbeat"),
                "last_remote_analysis_session": row.get("current_booking"),
                "last_user": row.get("current_user") or row.get("current_user_email"),
                "cpu": row.get("cpu"),
                "memory": row.get("memory"),
                "disk": row.get("disk"),
                "agent_id": row.get("agent_id"),
                "enabled": row.get("enabled"),
                "tunnel_status": row.get("tunnel_status"),
            }
        )

    # Summary counts
    all_nodes = []
    for d in departments.values():
        all_nodes.extend(d["nodes"])
        for n in d["nodes"]:
            all_nodes.extend(n.get("children") or [])

    status_counts: dict[str, int] = {}
    for n in all_nodes:
        status_counts[n["status"]] = status_counts.get(n["status"], 0) + 1

    return {
        "generated_at": timezone.now().isoformat(),
        "page": page,
        "page_size": page_size,
        "departments": [
            {
                "id": d["id"],
                "name": d["name"],
                "nodes": d["nodes"],
                "node_count": len(d["nodes"]) + sum(len(n.get("children") or []) for n in d["nodes"]),
            }
            for d in departments.values()
        ],
        "counts": {
            "total_nodes": len(all_nodes),
            "by_status": status_counts,
            "dsa": sum(1 for n in all_nodes if n["kind"] == LabNodeKind.DSA),
            "equipment_pc": sum(1 for n in all_nodes if n["kind"] == LabNodeKind.EQUIPMENT_PC),
            "analysis_pc": sum(1 for n in all_nodes if n["kind"] == LabNodeKind.ANALYSIS_PC),
        },
        "ra_fleet_counts": ra.get("counts") or {},
    }


def get_node_detail(node_id: str) -> dict[str, Any] | None:
    # Direct lookups — avoid rebuilding the full fleet tree on every detail/diagnostics call
    if node_id.startswith("raa:"):
        ws_pk = node_id.split(":", 1)[1]
        ws = AnalysisWorkstation.objects.filter(pk=ws_pk).first()
        if not ws:
            return None
        hb = WorkstationHeartbeat.objects.filter(workstation=ws).order_by("-received_at").first()
        raw = (hb.raw_payload if hb else None) or {}
        return {
            "id": node_id,
            "kind": LabNodeKind.ANALYSIS_PC,
            "computer_name": ws.hostname,
            "agent_version": ws.agent_version,
            "health_score": ws.health_score,
            "status": LabNodeStatus.ONLINE
            if ws.last_heartbeat
            and _age_seconds(ws.last_heartbeat) is not None
            and _age_seconds(ws.last_heartbeat) <= HEARTBEAT_OFFLINE_SECONDS
            else LabNodeStatus.OFFLINE,
            "last_heartbeat": ws.last_heartbeat.isoformat() if ws.last_heartbeat else None,
            "windows_version": ws.operating_system or ws.windows_version,
            "cpu": getattr(hb, "cpu", None),
            "memory": getattr(hb, "memory", None),
            "disk": getattr(hb, "disk", None),
            "disk_free_bytes": raw.get("diskFreeBytes") or raw.get("DiskFreeBytes"),
            "windows_uptime_seconds": raw.get("windowsUptimeSeconds"),
            "reverse_tunnel_status": raw.get("reverseTunnelStatus"),
            "raw_heartbeat": raw,
        }
    if node_id.startswith("dsa:"):
        agent = DepartmentSyncAgent.objects.filter(pk=node_id.split(":", 1)[1]).first()
        if not agent:
            return None
        pcs, hb = _latest_equipment_pcs(agent)
        return {
            "id": node_id,
            "kind": LabNodeKind.DSA,
            "computer_name": agent.machine_name or agent.agent_name,
            "agent_version": agent.version,
            "configuration_version": agent.last_reported_configuration_version,
            "last_heartbeat": agent.last_heartbeat_at.isoformat() if agent.last_heartbeat_at else None,
            "status": _dsa_status(agent, hb),
            "cpu": hb.cpu_percent if hb else None,
            "memory": hb.memory_percent if hb else None,
            "disk": hb.disk_percent if hb else None,
            "equipment_pcs": pcs,
            "windows_version": (hb.windows_build if hb else None) or agent.operating_system,
        }
    if node_id.startswith("eqpc:"):
        # Search recent DSA heartbeats for matching equipment PC id/mac
        key = node_id.split(":", 1)[1]
        for agent in DepartmentSyncAgent.objects.all().iterator():
            pcs, hb = _latest_equipment_pcs(agent)
            for pc in pcs:
                pid = str(pc.get("id") or pc.get("equipment_pc_id") or pc.get("mac_address") or pc.get("macAddress") or "")
                if pid == key or str(pc.get("mac_address") or pc.get("macAddress") or "") == key:
                    return {
                        "id": node_id,
                        "kind": LabNodeKind.EQUIPMENT_PC,
                        "computer_name": pc.get("hostname") or pc.get("computer_name"),
                        "status": _eqpc_status(pc, parent_online=_dsa_status(agent, hb) != LabNodeStatus.OFFLINE),
                        "ip_address": pc.get("observed_ip") or pc.get("observedIp") or pc.get("preferred_ip"),
                        "mac_address": pc.get("mac_address") or pc.get("macAddress"),
                        "parent_dsa_id": str(agent.pk),
                        "raw": pc,
                        "cpu": pc.get("cpu_percent") or pc.get("cpuPercent"),
                        "memory": pc.get("memory_percent") or pc.get("memoryPercent"),
                        "disk": pc.get("disk_used_percent") or pc.get("diskUsedPercent"),
                        "last_heartbeat": pc.get("last_status_at") or pc.get("last_seen"),
                    }
        return None
    return None
