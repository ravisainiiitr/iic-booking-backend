"""Lab Infrastructure API views."""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes, parser_classes
from rest_framework.parsers import FormParser, MultiPartParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.lab_infrastructure.models import (
    ConfigurationAck,
    ConfigurationChange,
    LabAlert,
    LabAuditEvent,
    LabRepairAction,
    SatTestCase,
    SatTestResult,
    SatTestRun,
)
from iic_booking.lab_infrastructure.services.fleet import build_infrastructure_tree, get_node_detail
from iic_booking.sync.authentication import DepartmentSyncAgentAuthentication
from iic_booking.sync.permissions import CanManageDepartmentSync, IsDepartmentSyncAgent

_MANAGE = [IsAuthenticated, CanManageDepartmentSync]


@api_view(["GET"])
@permission_classes(_MANAGE)
def infrastructure(request):
    department_id = request.query_params.get("department_id")
    dept = int(department_id) if department_id and str(department_id).isdigit() else None
    page = request.query_params.get("page")
    page_size = request.query_params.get("page_size")
    return Response(
        build_infrastructure_tree(
            department_id=dept,
            page=int(page) if page and str(page).isdigit() else 1,
            page_size=int(page_size) if page_size and str(page_size).isdigit() else 500,
        )
    )


@api_view(["GET"])
@permission_classes(_MANAGE)
def node_detail(request, node_id: str):
    detail = get_node_detail(node_id)
    if not detail:
        return Response({"detail": "Node not found."}, status=status.HTTP_404_NOT_FOUND)
    return Response(detail)


@api_view(["GET"])
@permission_classes(_MANAGE)
def alerts_list(request):
    qs = LabAlert.objects.filter(status__in=["open", "acknowledged"]).order_by("-created_at")[:200]
    # Also merge open DSA AlertEvents lightly
    from iic_booking.sync.models import AlertEvent

    dsa = []
    try:
        for a in AlertEvent.objects.filter(resolved_at__isnull=True).order_by("-created_at")[:50]:
            dsa.append(
                {
                    "id": f"dsa-alert:{a.pk}",
                    "code": getattr(a, "alert_code", None) or getattr(a, "code", "") or "dsa",
                    "severity": (getattr(a, "severity", None) or "warning").lower(),
                    "status": "open",
                    "title": getattr(a, "title", None) or getattr(a, "message", "")[:255],
                    "detail": getattr(a, "message", "") or "",
                    "source": "dsa",
                    "created_at": a.created_at.isoformat() if getattr(a, "created_at", None) else None,
                }
            )
    except Exception:
        dsa = []

    rows = [
        {
            "id": str(a.id),
            "code": a.code,
            "severity": a.severity,
            "status": a.status,
            "title": a.title,
            "detail": a.detail,
            "node_kind": a.node_kind,
            "node_id": a.node_id,
            "source": a.source,
            "created_at": a.created_at.isoformat(),
        }
        for a in qs
    ] + dsa
    return Response({"count": len(rows), "results": rows})


@api_view(["POST"])
@permission_classes(_MANAGE)
def alert_ack(request, alert_id: str):
    alert = LabAlert.objects.filter(pk=alert_id).first()
    if not alert:
        return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    alert.status = LabAlert.Status.ACKNOWLEDGED
    alert.save(update_fields=["status", "updated_at"])
    return Response({"id": str(alert.id), "status": alert.status})


@api_view(["GET"])
@permission_classes(_MANAGE)
def audit_list(request):
    qs = LabAuditEvent.objects.all().order_by("-created_at")[:200]
    # Merge SyncLog + WorkstationEvent samples
    merged = [
        {
            "id": str(e.id),
            "event_type": e.event_type,
            "message": e.message,
            "node_kind": e.node_kind,
            "node_id": e.node_id,
            "success": e.success,
            "created_at": e.created_at.isoformat(),
            "source": "lab",
        }
        for e in qs
    ]
    try:
        from iic_booking.sync.models import SyncLog

        for s in SyncLog.objects.order_by("-created_at")[:50]:
            merged.append(
                {
                    "id": f"sync:{s.pk}",
                    "event_type": getattr(s, "event_code", "") or "sync",
                    "message": getattr(s, "message", "") or "",
                    "success": True,
                    "created_at": s.created_at.isoformat() if getattr(s, "created_at", None) else None,
                    "source": "dsa",
                }
            )
    except Exception:
        pass
    try:
        from iic_booking.remote_analysis.models import WorkstationEvent

        for w in WorkstationEvent.objects.order_by("-created_at")[:50]:
            merged.append(
                {
                    "id": f"ra:{w.pk}",
                    "event_type": getattr(w, "action", "") or getattr(w, "category", "") or "ra",
                    "message": getattr(w, "details", "") or "",
                    "success": bool(getattr(w, "success", True)),
                    "created_at": w.created_at.isoformat() if getattr(w, "created_at", None) else None,
                    "source": "ra",
                }
            )
    except Exception:
        pass
    merged.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return Response({"count": len(merged), "results": merged[:200]})


@api_view(["POST"])
@permission_classes(_MANAGE)
def repair_action(request, node_id: str):
    action = (request.data.get("action") or "").strip()
    if action not in {c.value for c in LabRepairAction.Action}:
        return Response(
            {"detail": f"Invalid action. Choose one of: {[c.value for c in LabRepairAction.Action]}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    kind = "dsa"
    if node_id.startswith("raa:"):
        kind = "analysis_pc"
    elif node_id.startswith("eqpc:"):
        kind = "equipment_pc"
    elif node_id.startswith("dsa:"):
        kind = "dsa"

    row = LabRepairAction.objects.create(
        node_kind=kind,
        node_id=node_id,
        action=action,
        status=LabRepairAction.Status.QUEUED,
        requested_by=request.user,
    )

    # Map to existing agent command channels where possible
    result = {"queued": True}
    try:
        from iic_booking.sync.models import DepartmentSyncAgent

        if kind == "dsa" and action == LabRepairAction.Action.RESTART_AGENT:
            agent = DepartmentSyncAgent.objects.filter(pk=node_id.split(":", 1)[1]).first()
            if agent:
                agent.restart_required = True
                agent.save(update_fields=["restart_required", "updated_at"])
                result["restart_required"] = True
                row.status = LabRepairAction.Status.SENT
            else:
                row.status = LabRepairAction.Status.FAILED
                row.error_message = "DSA agent not found"
        elif kind == "dsa" and action in {
            LabRepairAction.Action.REFRESH_CONFIGURATION,
            LabRepairAction.Action.RECONFIGURE,
            LabRepairAction.Action.REPAIR,
            LabRepairAction.Action.RECOMMISSION,
        }:
            agent = DepartmentSyncAgent.objects.filter(pk=node_id.split(":", 1)[1]).first()
            if agent:
                agent.bootstrap_required = True
                agent.save(update_fields=["bootstrap_required", "updated_at"])
                result["bootstrap_required"] = True
                row.status = LabRepairAction.Status.SENT
            else:
                row.status = LabRepairAction.Status.FAILED
                row.error_message = "DSA agent not found"
        elif kind == "analysis_pc" and action == LabRepairAction.Action.RESCAN_SOFTWARE:
            result["note"] = "Rescan queued for next agent inventory cycle"
            row.status = LabRepairAction.Status.SENT
        else:
            row.status = LabRepairAction.Status.SENT
            result["note"] = "Action recorded; agent will pick up via command channel when available"
    except Exception as exc:
        row.status = LabRepairAction.Status.FAILED
        row.error_message = str(exc)
        result = {"error": str(exc)}

    row.result = result
    row.save(update_fields=["status", "result", "error_message", "updated_at"])

    LabAuditEvent.objects.create(
        event_type="repair_executed",
        message=f"{action} on {node_id}",
        node_kind=kind,
        node_id=node_id,
        actor=request.user,
        payload={"action": action, "result": result},
        success=row.status != LabRepairAction.Status.FAILED,
    )
    return Response(
        {"id": str(row.id), "status": row.status, "result": result},
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "POST"])
@permission_classes(_MANAGE)
def configuration_history(request, profile_id: str):
    from iic_booking.sync.models import EquipmentSyncProfile

    profile = EquipmentSyncProfile.objects.filter(pk=profile_id).first()
    if not profile:
        return Response({"detail": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        rows = ConfigurationChange.objects.filter(sync_profile=profile).order_by("-created_at")[:100]
        return Response(
            {
                "profile_id": str(profile.id),
                "current_version": profile.configuration_version,
                "results": [
                    {
                        "id": str(r.id),
                        "configuration_version": r.configuration_version,
                        "reason": r.reason,
                        "previous_value": r.previous_value,
                        "new_value": r.new_value,
                        "applied_by": getattr(r.applied_by, "email", None),
                        "created_at": r.created_at.isoformat(),
                    }
                    for r in rows
                ],
            }
        )

    # POST = record change + bump (or apply snapshot)
    reason = (request.data.get("reason") or "Manual configuration change").strip()
    new_value = request.data.get("new_value") or {}
    previous = {
        "share_name": profile.share_name,
        "watch_folder": profile.watch_folder,
        "unc_path": profile.unc_path,
        "sync_interval_seconds": profile.sync_interval_seconds,
        "enabled_features": profile.enabled_features,
        "configuration_version": profile.configuration_version,
    }
    for key in ("share_name", "watch_folder", "unc_path", "sync_interval_seconds"):
        if key in new_value:
            setattr(profile, key, new_value[key])
    if "enabled_features" in new_value and isinstance(new_value["enabled_features"], dict):
        profile.enabled_features = new_value["enabled_features"]
    # Persist field changes then bump version (bump alone only saves configuration_version)
    profile.configuration_version = (profile.configuration_version or 0) + 1
    profile.save(
        update_fields=[
            "share_name",
            "watch_folder",
            "unc_path",
            "sync_interval_seconds",
            "enabled_features",
            "configuration_version",
            "updated_at",
        ]
    )
    change = ConfigurationChange.objects.create(
        sync_profile=profile,
        configuration_version=profile.configuration_version,
        previous_value=previous,
        new_value=new_value or {"bumped": True},
        reason=reason,
        applied_by=request.user,
    )
    for assignment in profile.assignments.filter(is_active=True).select_related("sync_agent"):
        if assignment.sync_agent:
            assignment.sync_agent.bootstrap_required = True
            assignment.sync_agent.save(update_fields=["bootstrap_required", "updated_at"])
            ConfigurationAck.objects.update_or_create(
                sync_agent=assignment.sync_agent,
                sync_profile=profile,
                configuration_version=profile.configuration_version,
                equipment_pc_id="",
                defaults={"status": ConfigurationAck.Status.PENDING, "error_message": ""},
            )
    LabAuditEvent.objects.create(
        event_type="configuration_applied",
        message=f"Profile {profile.id} → v{profile.configuration_version}",
        actor=request.user,
        payload={"change_id": str(change.id), "reason": reason},
    )
    return Response(
        {"configuration_version": profile.configuration_version, "change_id": str(change.id)},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes(_MANAGE)
def configuration_rollback(request, profile_id: str):
    from iic_booking.sync.models import EquipmentSyncProfile

    profile = EquipmentSyncProfile.objects.filter(pk=profile_id).first()
    if not profile:
        return Response({"detail": "Profile not found"}, status=status.HTTP_404_NOT_FOUND)
    change_id = request.data.get("change_id")
    change = ConfigurationChange.objects.filter(pk=change_id, sync_profile=profile).first()
    if not change:
        return Response({"detail": "Change not found"}, status=status.HTTP_404_NOT_FOUND)
    prev = change.previous_value or {}
    snapshot_before = {
        "share_name": profile.share_name,
        "watch_folder": profile.watch_folder,
        "unc_path": profile.unc_path,
        "sync_interval_seconds": profile.sync_interval_seconds,
        "enabled_features": profile.enabled_features,
    }
    for key in ("share_name", "watch_folder", "unc_path", "sync_interval_seconds"):
        if key in prev:
            setattr(profile, key, prev[key])
    if "enabled_features" in prev:
        profile.enabled_features = prev["enabled_features"]
    profile.configuration_version = (profile.configuration_version or 0) + 1
    profile.save(
        update_fields=[
            "share_name",
            "watch_folder",
            "unc_path",
            "sync_interval_seconds",
            "enabled_features",
            "configuration_version",
            "updated_at",
        ]
    )
    ConfigurationChange.objects.create(
        sync_profile=profile,
        configuration_version=profile.configuration_version,
        previous_value=snapshot_before,
        new_value=prev,
        reason=f"Rollback to change {change.id}",
        applied_by=request.user,
    )
    for assignment in profile.assignments.filter(is_active=True).select_related("sync_agent"):
        if assignment.sync_agent:
            assignment.sync_agent.bootstrap_required = True
            assignment.sync_agent.save(update_fields=["bootstrap_required", "updated_at"])
    return Response({"configuration_version": profile.configuration_version, "rolled_back_from": str(change.id)})


@api_view(["POST"])
@authentication_classes([DepartmentSyncAgentAuthentication])
@permission_classes([IsDepartmentSyncAgent])
def configuration_ack(request):
    """DSA posts config apply acknowledgement (agent bearer auth)."""
    version = request.data.get("configuration_version")
    equipment_pc_id = (request.data.get("equipment_pc_id") or "")[:64]
    ack_status = (request.data.get("status") or "applied").lower()
    error = request.data.get("error_message") or ""
    if version is None:
        return Response({"detail": "configuration_version required"}, status=status.HTTP_400_BAD_REQUEST)

    sync_agent = getattr(request, "sync_agent", None) or getattr(request.user, "agent", None)
    if sync_agent is None:
        return Response({"detail": "Authenticated sync agent required."}, status=status.HTTP_400_BAD_REQUEST)
    st = ConfigurationAck.Status.APPLIED if ack_status == "applied" else ConfigurationAck.Status.FAILED
    ack, _created = ConfigurationAck.objects.update_or_create(
        sync_agent=sync_agent,
        configuration_version=int(version),
        equipment_pc_id=equipment_pc_id,
        defaults={
            "status": st,
            "error_message": error,
            "applied_at": timezone.now() if st == ConfigurationAck.Status.APPLIED else None,
        },
    )
    return Response({"id": str(ack.id), "status": ack.status}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes(_MANAGE)
def run_diagnostics(request, node_id: str):
    checks = []
    detail = get_node_detail(node_id)
    if not detail:
        return Response({"detail": "Node not found"}, status=status.HTTP_404_NOT_FOUND)

    def add(name, passed, message, severity="info"):
        checks.append({"name": name, "passed": passed, "message": message, "severity": severity})

    add("NodeResolved", True, f"Resolved {detail.get('kind')} {detail.get('computer_name')}")
    hb = detail.get("last_heartbeat")
    add("Heartbeat", bool(hb), f"Last heartbeat: {hb or 'never'}", "error" if not hb else "info")
    if detail.get("kind") in {"dsa", "analysis_pc"}:
        add("AgentVersion", bool(detail.get("agent_version")), f"Version {detail.get('agent_version') or 'unknown'}")
    if detail.get("cpu") is not None:
        add("CPU", float(detail["cpu"]) < 95, f"CPU {detail['cpu']}%", "warning" if float(detail["cpu"]) >= 90 else "info")
    if detail.get("disk") is not None:
        add("Disk", float(detail["disk"]) < 95, f"Disk {detail['disk']}%", "critical" if float(detail["disk"]) >= 95 else "info")
    if detail.get("reverse_tunnel_status"):
        tun = str(detail["reverse_tunnel_status"]).upper()
        add("ReverseTunnel", tun in {"CONNECTED", "UNKNOWN", "DISABLED"}, f"Tunnel {tun}")

    # Lightweight node-scoped checks only (full fleet commissioning is a separate ops action)
    if node_id.startswith("raa:"):
        try:
            from iic_booking.remote_analysis.models import AnalysisWorkstation

            ws = AnalysisWorkstation.objects.filter(pk=node_id.split(":", 1)[1]).first()
            add("WorkstationRecord", ws is not None, f"Workstation {'found' if ws else 'missing'}")
            if ws:
                add("Enabled", bool(ws.enabled), f"enabled={ws.enabled}")
                add(
                    "Fingerprint",
                    bool(getattr(ws, "machine_fingerprint", None)),
                    f"fingerprint={getattr(ws, 'machine_fingerprint', None) or 'empty'}",
                )
        except Exception as exc:
            add("WorkstationLookup", False, str(exc), "warning")

    failed = [c for c in checks if not c["passed"]]
    overall = "FAIL" if any(c["severity"] == "critical" and not c["passed"] for c in checks) else (
        "WARNINGS" if failed else "HEALTHY"
    )
    LabAuditEvent.objects.create(
        event_type="diagnostics_run",
        message=f"Diagnostics {overall} for {node_id}",
        node_id=node_id,
        actor=request.user,
        payload={"overall": overall, "checks": checks},
        success=overall != "FAIL",
    )
    return Response({"node_id": node_id, "overall": overall, "checks": checks, "generated_at": timezone.now().isoformat()})


@api_view(["GET"])
@permission_classes(_MANAGE)
def software_compliance(request):
    """Required vs installed software matrix for Analysis PCs."""
    from iic_booking.remote_analysis.models import AnalysisWorkstation, InstalledSoftware

    rows = []
    for ws in AnalysisWorkstation.objects.filter(enabled=True).order_by("hostname")[:200]:
        installed = list(
            InstalledSoftware.objects.filter(workstation=ws).values_list("name", "version")[:100]
        )
        installed_map = {n.lower(): v for n, v in installed}
        required = []
        # Pull from linked equipment requirements if available
        try:
            from iic_booking.remote_analysis.scheduler_models import SoftwareRequirement

            for req in SoftwareRequirement.objects.all()[:50]:
                name = getattr(req, "name", None) or getattr(req, "software_name", "")
                if not name:
                    continue
                ver = installed_map.get(name.lower())
                required.append(
                    {
                        "name": name,
                        "required_version": getattr(req, "min_version", None) or getattr(req, "version", "") or "",
                        "installed_version": ver,
                        "status": "installed" if ver else "missing",
                    }
                )
        except Exception:
            required = []
        rows.append(
            {
                "workstation_id": str(ws.id),
                "hostname": ws.hostname,
                "installed_count": len(installed),
                "requirements": required,
                "missing": sum(1 for r in required if r["status"] == "missing"),
            }
        )
    return Response({"count": len(rows), "results": rows})


@api_view(["GET"])
@permission_classes(_MANAGE)
def utilization_report(request):
    """Lightweight utilization snapshot (CSV-friendly JSON)."""
    from iic_booking.remote_analysis.models import AnalysisWorkstation

    fmt = (request.query_params.get("format") or "json").lower()
    ws_count = AnalysisWorkstation.objects.count()
    online = AnalysisWorkstation.objects.filter(
        last_heartbeat__gte=timezone.now() - timedelta(minutes=5)
    ).count()
    from iic_booking.sync.models import DepartmentSyncAgent

    dsa_count = DepartmentSyncAgent.objects.count()
    payload = {
        "generated_at": timezone.now().isoformat(),
        "analysis_pcs": {"total": ws_count, "recently_online": online},
        "dsa_agents": {"total": dsa_count},
        "report_type": "utilization_snapshot",
    }
    if fmt == "csv":
        lines = [
            "metric,value",
            f"analysis_pcs_total,{ws_count}",
            f"analysis_pcs_online,{online}",
            f"dsa_total,{dsa_count}",
        ]
        from django.http import HttpResponse

        resp = HttpResponse("\n".join(lines), content_type="text/csv")
        resp["Content-Disposition"] = 'attachment; filename="lab-utilization.csv"'
        return resp
    return Response(payload)


@api_view(["POST"])
@permission_classes(_MANAGE)
def maintenance_action(request, node_id: str):
    """Surface existing RA maintenance / DSA drain without new orchestration stacks."""
    action = (request.data.get("action") or "").strip().lower()
    LabAuditEvent.objects.create(
        event_type="maintenance_action",
        message=f"{action} on {node_id}",
        node_id=node_id,
        actor=request.user,
        payload=dict(request.data),
    )
    if node_id.startswith("raa:") and action in {"take_offline", "resume", "schedule"}:
        try:
            from iic_booking.remote_analysis.models import AnalysisWorkstation
            from iic_booking.remote_analysis.constants import WorkstationStatus

            ws = AnalysisWorkstation.objects.filter(pk=node_id.split(":", 1)[1]).first()
            if not ws:
                return Response({"detail": "Workstation not found"}, status=status.HTTP_404_NOT_FOUND)
            if action == "take_offline":
                ws.status = WorkstationStatus.MAINTENANCE
                ws.save(update_fields=["status", "updated_at"])
            elif action == "resume":
                ws.status = WorkstationStatus.ONLINE
                ws.enabled = True
                ws.save(update_fields=["status", "enabled", "updated_at"])
            return Response({"node_id": node_id, "action": action, "status": ws.status})
        except Exception as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    if node_id.startswith("dsa:") and action in {"drain", "maintenance", "resume"}:
        from iic_booking.sync.models import DepartmentSyncAgent, AgentLifecycleStatus

        agent = DepartmentSyncAgent.objects.filter(pk=node_id.split(":", 1)[1]).first()
        if not agent:
            return Response({"detail": "Agent not found"}, status=status.HTTP_404_NOT_FOUND)
        if action == "drain":
            agent.status = AgentLifecycleStatus.DRAINING
        elif action == "maintenance":
            agent.status = AgentLifecycleStatus.MAINTENANCE
        else:
            agent.status = AgentLifecycleStatus.ACTIVE
        agent.save(update_fields=["status", "updated_at"])
        return Response({"node_id": node_id, "action": action, "status": agent.status})
    return Response({"detail": f"Unsupported action {action} for {node_id}"}, status=status.HTTP_400_BAD_REQUEST)


@api_view(["POST"])
@permission_classes(_MANAGE)
def rotate_agent_secret_hint(request, node_id: str):
    """Trigger DSA API-key rotate path flag (credential rotation workflow entry)."""
    if not node_id.startswith("dsa:"):
        return Response({"detail": "Only DSA nodes support secret rotation hint"}, status=400)
    from iic_booking.sync.models import DepartmentSyncAgent

    agent = DepartmentSyncAgent.objects.filter(pk=node_id.split(":", 1)[1]).first()
    if not agent:
        return Response({"detail": "Not found"}, status=404)
    # Existing security/api-keys/rotate is agent-initiated; mark bootstrap + audit for admin-driven rotation request
    agent.bootstrap_required = True
    agent.save(update_fields=["bootstrap_required", "updated_at"])
    LabAuditEvent.objects.create(
        event_type="credential_rotation_requested",
        message=f"Admin requested credential rotation for {node_id}",
        node_id=node_id,
        actor=request.user,
    )
    return Response({"detail": "Rotation requested; agent should call security/api-keys/rotate on next cycle."})


def _is_main_admin(user) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return str(getattr(user, "user_type", "") or "").lower() == "admin"


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def testing_dashboard(request):
    """Main Admin Lab SAT Execution Dashboard."""
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.sat_execution import (
        compute_readiness,
        current_wizard_step,
        live_health_panel,
        run_progress,
        stage_summary,
    )
    from iic_booking.lab_infrastructure.services.testing import module_summary, overall_health

    run_id = request.query_params.get("run_id")
    run = SatTestRun.objects.filter(pk=run_id).first() if run_id else SatTestRun.objects.order_by("-started_at").first()
    modules = module_summary(run)
    progress = run_progress(run) if run else None
    readiness = compute_readiness(run) if run else None
    return Response(
        {
            "overall": overall_health(modules),
            "modules": modules,
            "progress": progress,
            "stages": stage_summary(run) if run else [],
            "readiness": readiness,
            "current_test": current_wizard_step(run) if run and run.status == SatTestRun.Status.RUNNING else None,
            "health_panel": live_health_panel(),
            "run_id": str(run.id) if run else None,
            "latest_run": _serialize_run(run),
            "mode": "lab_sat_execution",
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def testing_runs(request):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.testing import ensure_catalog, start_run

    if request.method == "GET":
        ensure_catalog()
        runs = SatTestRun.objects.order_by("-started_at")[:50]
        return Response({"count": len(list(runs)), "results": [_serialize_run(r) for r in runs]})

    lab_context = request.data.get("lab_context") if isinstance(request.data.get("lab_context"), dict) else {}
    run = start_run(
        user=request.user,
        name=(request.data.get("name") or "")[:255],
        suite=(request.data.get("suite") or "")[:16],
        lab_context=lab_context,
    )
    return Response(_serialize_run(run), status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def testing_run_detail(request, run_id):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    run = SatTestRun.objects.filter(pk=run_id).first()
    if not run:
        return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "PATCH":
        st = (request.data.get("status") or "").lower()
        if st in {c.value for c in SatTestRun.Status}:
            run.status = st
            if st in {SatTestRun.Status.COMPLETED, SatTestRun.Status.ABORTED}:
                run.finished_at = timezone.now()
            run.save(update_fields=["status", "finished_at"])
        if "notes" in request.data:
            run.notes = str(request.data.get("notes") or "")[:5000]
            run.save(update_fields=["notes"])
    return Response(_serialize_run(run))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def testing_results(request):
    """Drill-down results; filter by module / status / run / failed-only / stage."""
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.sat_execution import serialize_result
    from iic_booking.lab_infrastructure.services.testing import ensure_catalog

    ensure_catalog()
    run_id = request.query_params.get("run_id")
    run = SatTestRun.objects.filter(pk=run_id).first() if run_id else SatTestRun.objects.order_by("-started_at").first()
    qs = SatTestResult.objects.select_related("test_case", "run").prefetch_related("evidence_files", "defects")
    if run:
        qs = qs.filter(run=run)
    else:
        qs = qs.none()
    module = (request.query_params.get("module") or "").strip()
    if module:
        qs = qs.filter(test_case__module=module)
    stage = request.query_params.get("stage")
    if stage and str(stage).isdigit():
        qs = qs.filter(test_case__stage=int(stage))
    st = (request.query_params.get("status") or "").strip().lower()
    if st:
        qs = qs.filter(status=st)
    if str(request.query_params.get("failed_only") or "") in {"1", "true", "yes"}:
        qs = qs.filter(status__in=[SatTestResult.Status.FAILED, SatTestResult.Status.BLOCKED])
    qs = qs.order_by("test_case__stage", "test_case__execution_order", "test_case__test_id")
    results = [serialize_result(r) for r in qs[:500]]
    return Response({"count": len(results), "run_id": str(run.id) if run else None, "results": results})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def testing_result_update(request, result_id):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.sat_execution import record_result, serialize_result

    result = (
        SatTestResult.objects.select_related("test_case", "run")
        .prefetch_related("evidence_files", "defects")
        .filter(pk=result_id)
        .first()
    )
    if not result:
        return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    st = (request.data.get("status") or "").lower()
    if st and st not in {c.value for c in SatTestResult.Status}:
        return Response({"detail": "Invalid status"}, status=status.HTTP_400_BAD_REQUEST)
    if st:
        record_result(
            result,
            status=st,
            actual_result=str(request.data.get("actual_result") or ""),
            remarks=str(request.data.get("remarks") or ""),
            administrator_notes=str(request.data.get("administrator_notes") or ""),
            log_url=str(request.data.get("log_url") or ""),
            advance=str(request.data.get("advance") or "1") not in {"0", "false", "no"},
        )
    else:
        if "actual_result" in request.data:
            result.actual_result = str(request.data.get("actual_result") or "")[:8000]
        if "remarks" in request.data:
            result.remarks = str(request.data.get("remarks") or "")[:4000]
        if "administrator_notes" in request.data:
            result.administrator_notes = str(request.data.get("administrator_notes") or "")[:8000]
        if "log_url" in request.data:
            result.log_url = str(request.data.get("log_url") or "")[:500]
        result.save()
    result.refresh_from_db()
    LabAuditEvent.objects.create(
        event_type="sat_result_updated",
        message=f"{result.test_case.test_id} → {result.status}",
        actor=request.user,
        payload={"result_id": str(result.id), "status": result.status},
        success=result.status != SatTestResult.Status.FAILED,
    )
    from iic_booking.lab_infrastructure.services.sat_execution import current_wizard_step

    return Response(
        {
            **serialize_result(result),
            "next_test": current_wizard_step(result.run) if result.run.status == SatTestRun.Status.RUNNING else None,
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def testing_seed_catalog(request):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.testing import ensure_catalog

    created = ensure_catalog()
    total = SatTestCase.objects.filter(is_active=True).count()
    return Response({"created": created, "total": total})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def testing_wizard_current(request):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.sat_execution import current_wizard_step, run_progress

    run_id = request.query_params.get("run_id")
    run = SatTestRun.objects.filter(pk=run_id).first() if run_id else SatTestRun.objects.order_by("-started_at").first()
    if not run:
        return Response({"detail": "No SAT run"}, status=status.HTTP_404_NOT_FOUND)
    return Response({"progress": run_progress(run), "current_test": current_wizard_step(run)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([FormParser, MultiPartParser])
def testing_evidence_upload(request):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.models import SatEvidence

    run_id = request.data.get("run_id")
    result_id = request.data.get("result_id")
    run = SatTestRun.objects.filter(pk=run_id).first()
    if not run:
        return Response({"detail": "run_id required"}, status=status.HTTP_400_BAD_REQUEST)
    result = SatTestResult.objects.filter(pk=result_id, run=run).first() if result_id else None
    upload = request.FILES.get("file")
    if not upload:
        return Response({"detail": "file required"}, status=status.HTTP_400_BAD_REQUEST)
    kind = (request.data.get("kind") or SatEvidence.Kind.OTHER).lower()
    if kind not in {c.value for c in SatEvidence.Kind}:
        kind = SatEvidence.Kind.OTHER
    ev = SatEvidence.objects.create(
        run=run,
        result=result,
        kind=kind,
        title=(request.data.get("title") or upload.name or "")[:255],
        file=upload,
        original_name=(upload.name or "")[:255],
        content_type=getattr(upload, "content_type", "") or "",
        uploaded_by=request.user,
    )
    return Response(
        {
            "id": str(ev.id),
            "kind": ev.kind,
            "title": ev.title,
            "original_name": ev.original_name,
            "url": ev.file.url if ev.file else "",
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def testing_defects(request):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.models import SatDefect

    if request.method == "GET":
        run_id = request.query_params.get("run_id")
        qs = SatDefect.objects.all().order_by("-created_at")
        if run_id:
            qs = qs.filter(run_id=run_id)
        return Response(
            {
                "count": qs.count(),
                "results": [
                    {
                        "id": str(d.id),
                        "run_id": str(d.run_id),
                        "result_id": str(d.result_id) if d.result_id else None,
                        "kind": d.kind,
                        "severity": d.severity,
                        "status": d.status,
                        "title": d.title,
                        "description": d.description,
                        "test_id": d.test_id,
                        "equipment_id": d.equipment_id,
                        "department_id": d.department_id,
                        "machine_name": d.machine_name,
                        "node_id": d.node_id,
                        "created_at": d.created_at.isoformat() if d.created_at else None,
                    }
                    for d in qs[:200]
                ],
            }
        )

    run = SatTestRun.objects.filter(pk=request.data.get("run_id")).first()
    if not run:
        return Response({"detail": "run_id required"}, status=status.HTTP_400_BAD_REQUEST)
    result = None
    if request.data.get("result_id"):
        result = SatTestResult.objects.filter(pk=request.data.get("result_id"), run=run).first()
    title = (request.data.get("title") or "").strip()
    if not title:
        return Response({"detail": "title required"}, status=status.HTTP_400_BAD_REQUEST)
    kind = (request.data.get("kind") or SatDefect.Kind.BUG).lower()
    if kind not in {c.value for c in SatDefect.Kind}:
        kind = SatDefect.Kind.BUG
    sev = (request.data.get("severity") or SatDefect.Severity.HIGH).lower()
    if sev not in {c.value for c in SatDefect.Severity}:
        sev = SatDefect.Severity.HIGH
    defect = SatDefect.objects.create(
        run=run,
        result=result,
        kind=kind,
        severity=sev,
        title=title[:255],
        description=str(request.data.get("description") or "")[:8000],
        test_id=(request.data.get("test_id") or (result.test_case.test_id if result else ""))[:32],
        equipment_id=str(request.data.get("equipment_id") or "")[:64],
        department_id=request.data.get("department_id") if str(request.data.get("department_id") or "").isdigit() else None,
        machine_name=str(request.data.get("machine_name") or "")[:255],
        node_id=str(request.data.get("node_id") or "")[:64],
        created_by=request.user,
    )
    LabAuditEvent.objects.create(
        event_type="sat_defect_created",
        message=f"SAT defect {defect.title}",
        actor=request.user,
        payload={"defect_id": str(defect.id), "test_id": defect.test_id, "severity": defect.severity},
        success=False,
    )
    return Response({"id": str(defect.id), "title": defect.title, "severity": defect.severity}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def testing_report(request, run_id):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.sat_execution import (
        build_report_payload,
        compute_readiness,
        report_csv,
        report_pdf,
        report_xlsx,
    )

    run = SatTestRun.objects.filter(pk=run_id).first()
    if not run:
        return Response({"detail": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    fmt = (request.query_params.get("format") or "json").lower()
    if run.status == SatTestRun.Status.COMPLETED or request.query_params.get("refresh") == "1":
        readiness = compute_readiness(run)
        run.readiness_snapshot = readiness
        run.recommendation = readiness.get("recommendation", run.recommendation)
        run.save(update_fields=["readiness_snapshot", "recommendation"])
    if fmt == "csv":
        return report_csv(run)
    if fmt in {"xlsx", "excel"}:
        return report_xlsx(run)
    if fmt == "pdf":
        return report_pdf(run)
    return Response(build_report_payload(run))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def testing_health_panel(request):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.sat_execution import live_health_panel

    return Response(live_health_panel())


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def testing_readiness(request):
    if not _is_main_admin(request.user):
        return Response({"detail": "Main Administrator only."}, status=status.HTTP_403_FORBIDDEN)
    from iic_booking.lab_infrastructure.services.sat_execution import compute_readiness

    run_id = request.query_params.get("run_id")
    run = SatTestRun.objects.filter(pk=run_id).first() if run_id else SatTestRun.objects.order_by("-started_at").first()
    if not run:
        return Response({"detail": "No SAT run"}, status=status.HTTP_404_NOT_FOUND)
    readiness = compute_readiness(run)
    run.readiness_snapshot = readiness
    run.recommendation = readiness.get("recommendation", run.recommendation)
    run.save(update_fields=["readiness_snapshot", "recommendation"])
    return Response(readiness)


def _serialize_run(run: SatTestRun | None) -> dict | None:
    if run is None:
        return None
    return {
        "id": str(run.id),
        "name": run.name,
        "suite": run.suite,
        "status": run.status,
        "notes": run.notes,
        "recommendation": run.recommendation,
        "lab_context": run.lab_context or {},
        "current_result_id": str(run.current_result_id) if run.current_result_id else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "executed_by": getattr(run.executed_by, "email", None) or getattr(run.executed_by, "username", None),
    }
