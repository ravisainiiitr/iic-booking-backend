"""Fleet health inventory + RAW/RESULTS configuration audit."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from django.db.models import Q
from django.utils import timezone

from iic_booking.remote_analysis.constants import HEARTBEAT_OFFLINE_SECONDS, ReservationStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware, WorkstationHeartbeat
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.tunnel_models import TunnelSession


def fleet_inventory(*, department_id: int | None = None, status: str | None = None) -> dict[str, Any]:
    qs = AnalysisWorkstation.objects.all().order_by("hostname", "agent_id")
    if department_id is not None:
        qs = qs.filter(Q(department_id=department_id) | Q(department_id__isnull=True))
    if status:
        qs = qs.filter(status=status.upper())

    now = timezone.now()
    active_res = {
        r.workstation_id: r
        for r in AnalysisReservation.objects.filter(
            workstation_id__isnull=False,
            status__in=[
                ReservationStatus.AWAITING_CHECKIN,
                ReservationStatus.RESERVED,
                ReservationStatus.PREPARING,
                ReservationStatus.READY,
                ReservationStatus.ACTIVE,
            ],
        ).select_related("user", "booking")
    }
    open_tunnels = {
        t.workstation_id: t
        for t in TunnelSession.objects.exclude(status__in=["CLOSED", "FAILED", "EXPIRED"]).select_related(
            "workstation"
        )
    }

    # Load present software in bulk (avoid N+1 and Prefetch slice quirks)
    present_by_ws: dict = {}
    for s in InstalledSoftware.objects.filter(
        workstation_id__in=qs.values_list("id", flat=True), is_present=True
    ).order_by("software_name")[:5000]:
        present_by_ws.setdefault(s.workstation_id, []).append(s)

    from iic_booking.remote_analysis.models import SoftwareLicense

    license_by_ws: dict = {}
    for lic in SoftwareLicense.objects.filter(workstation_id__in=qs.values_list("id", flat=True)).order_by(
        "software"
    )[:5000]:
        license_by_ws.setdefault(lic.workstation_id, []).append(lic)

    rows = []
    for ws in qs.select_related("department"):
        hb = (
            WorkstationHeartbeat.objects.filter(workstation=ws)
            .order_by("-received_at")
            .first()
        )
        res = active_res.get(ws.id)
        tun = open_tunnels.get(ws.id)
        inventory_age = None
        if ws.last_inventory_update:
            inventory_age = int((now - ws.last_inventory_update).total_seconds())
        hb_age = None
        if ws.last_heartbeat:
            hb_age = int((now - ws.last_heartbeat).total_seconds())
        soft_list = present_by_ws.get(ws.id, [])[:40]
        software_rows = [
            {
                "name": s.software_name,
                "version": s.version,
                "publisher": s.publisher,
                "licensed": s.licensed,
                "license_type": s.license_type,
            }
            for s in soft_list
        ]
        license_rows = [
            {
                "software": lic.software,
                "status": lic.status,
                "seats": lic.seats,
                "license_server": lic.license_server,
            }
            for lic in license_by_ws.get(ws.id, [])[:40]
        ]
        rows.append(
            {
                "id": str(ws.id),
                "hostname": ws.hostname,
                "display_name": ws.display_name,
                "agent_id": ws.agent_id,
                "machine_fingerprint": ws.machine_fingerprint,
                "status": ws.status,
                "enabled": ws.enabled,
                "health_score": ws.health_score,
                "agent_version": ws.agent_version,
                "operating_system": ws.operating_system or ws.windows_version,
                "department_id": ws.department_id,
                "department_name": getattr(ws.department, "name", None) if ws.department_id else ws.department_name,
                "cpu_model": ws.cpu,
                "cpu_cores": ws.cpu_cores,
                "memory_gb": ws.memory_gb,
                "storage_gb": ws.storage_gb,
                "gpu": ws.gpu,
                "last_heartbeat": ws.last_heartbeat.isoformat() if ws.last_heartbeat else None,
                "last_seen": ws.last_heartbeat.isoformat() if ws.last_heartbeat else None,
                "heartbeat_age_seconds": hb_age,
                "online": bool(hb_age is not None and hb_age <= HEARTBEAT_OFFLINE_SECONDS),
                "software_inventory_age_seconds": inventory_age,
                "cpu": getattr(hb, "cpu", None),
                "memory": getattr(hb, "memory", None),
                "disk": getattr(hb, "disk", None),
                "load": {
                    "cpu": getattr(hb, "cpu", None),
                    "memory": getattr(hb, "memory", None),
                    "disk": getattr(hb, "disk", None),
                    "gpu": getattr(hb, "gpu", None),
                },
                "current_user": (
                    (getattr(hb, "logged_in_user", None) or (hb.raw_payload or {}).get("LoggedInUser"))
                    if hb
                    else None
                ),
                "current_booking": getattr(getattr(res, "booking", None), "virtual_booking_id", None)
                or (str(res.booking_id) if res and res.booking_id else None),
                "current_user_email": getattr(getattr(res, "user", None), "email", None),
                "session_status": res.status if res else None,
                "reservation_status": res.status if res else None,
                "tunnel_status": tun.status if tun else None,
                "rdp_status": "SESSION_ACTIVE"
                if res and res.status == ReservationStatus.ACTIVE
                else ("RESERVED" if res else "IDLE"),
                "workspace_status": None,
                "installed_software": software_rows,
                "installed_software_count": len(present_by_ws.get(ws.id, [])),
                "licenses": license_rows,
            }
        )

    counts = {
        "total": qs.count(),
        "online": sum(1 for r in rows if r["online"]),
        "offline": sum(1 for r in rows if r["status"] == WorkstationStatus.OFFLINE or not r["online"]),
        "busy": sum(1 for r in rows if r["status"] in {WorkstationStatus.BUSY, WorkstationStatus.PREPARING}),
        "available": sum(1 for r in rows if r["status"] in {WorkstationStatus.AVAILABLE, WorkstationStatus.ONLINE}),
        "reserved": sum(1 for r in rows if r["status"] == WorkstationStatus.RESERVED),
        "maintenance": sum(1 for r in rows if r["status"] == WorkstationStatus.MAINTENANCE),
        "calibration": sum(1 for r in rows if r["status"] == WorkstationStatus.CALIBRATION),
        "software_update": sum(1 for r in rows if r["status"] == WorkstationStatus.SOFTWARE_UPDATE),
        "hardware_fault": sum(1 for r in rows if r["status"] == WorkstationStatus.HARDWARE_FAULT),
        "disabled": sum(1 for r in rows if r["status"] == WorkstationStatus.DISABLED or not r["enabled"]),
        "fault": sum(
            1 for r in rows if r["status"] in {WorkstationStatus.HARDWARE_FAULT, WorkstationStatus.ERROR}
        ),
    }
    return {"counts": counts, "workstations": rows, "generated_at": now.isoformat()}


def equipment_ra_config_audit() -> dict[str, Any]:
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.remote_analysis_integration.software import SoftwareMappingService

    rows = []
    qs = Equipment.objects.filter(enable_remote_analysis=True).order_by("code")
    for eq in qs:
        soft = SoftwareMappingService().required_software_names(eq)
        missing = []
        if not (eq.analysis_raw_data_directory or "").strip():
            missing.append("RAW Directory")
        if not (eq.analysis_results_directory or "").strip():
            missing.append("RESULTS Directory")
        if not soft:
            missing.append("Required Software")
        if not eq.analysis_default_session_minutes:
            missing.append("Default Session Duration")
        if not eq.analysis_extension_minutes:
            missing.append("Extension Duration")
        rows.append(
            {
                "equipment_id": eq.equipment_id,
                "code": eq.code,
                "name": eq.name,
                "raw_directory": eq.analysis_raw_data_directory or "",
                "results_directory": eq.analysis_results_directory or "",
                "required_software": soft,
                "default_session_minutes": eq.analysis_default_session_minutes,
                "extension_minutes": eq.analysis_extension_minutes,
                "checkin_minutes": getattr(eq, "analysis_checkin_minutes", 10),
                "missing": missing,
                "complete": len(missing) == 0,
            }
        )
    incomplete = [r for r in rows if not r["complete"]]
    return {
        "total_ra_equipment": len(rows),
        "complete": len(rows) - len(incomplete),
        "incomplete": len(incomplete),
        "equipment": rows,
        "incomplete_equipment": incomplete,
    }
