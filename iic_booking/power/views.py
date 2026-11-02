"""UPS monitoring endpoints: the sync agent's config and readings (agent-authenticated, under /api/v1/sync/ups/), and
the staff view of UPS health (/api/power/ups/)."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, authentication_classes, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.sync.authentication import DepartmentSyncAgentAuthentication
from iic_booking.sync.permissions import IsDepartmentSyncAgent

from . import service
from .models import UpsDevice, UpsReading
from .register_maps import points_for


@api_view(["GET"])
@authentication_classes([DepartmentSyncAgentAuthentication])
@permission_classes([IsDepartmentSyncAgent])
def agent_ups_devices(request):
    """The UPS units this agent polls, with the Modbus points to read."""
    rows = []
    for d in UpsDevice.objects.filter(sync_agent=request.sync_agent, enabled=True):
        rows.append({"device_id": d.pk, "name": d.name, "host": d.host, "port": d.port, "unit_id": d.unit_id,
                     "poll_seconds": max(10, d.poll_seconds), "points": points_for(d)})
    return Response({"devices": rows})


@api_view(["POST"])
@authentication_classes([DepartmentSyncAgentAuthentication])
@permission_classes([IsDepartmentSyncAgent])
def agent_ups_readings(request):
    """Body: {"readings": [{device_id, read_at, reachable, error?, values: {name: value}}]}."""
    items = request.data.get("readings") if isinstance(request.data, dict) else None
    if not isinstance(items, list):
        return Response({"error": "readings must be a list."}, status=status.HTTP_400_BAD_REQUEST)
    return Response(service.record(request.sync_agent, items))


def _visible_devices(user):
    from iic_booking.users.models.user_type import UserType

    qs = UpsDevice.objects.select_related("department", "sync_agent").prefetch_related("equipment")
    if getattr(user, "is_superuser", False) or user.user_type == UserType.ADMIN:
        return qs
    if user.user_type == UserType.DEPT_ADMIN:
        return qs.filter(department_id=user.department_id)
    if user.user_type in (UserType.MANAGER, UserType.OPERATOR):
        from iic_booking.equipment.models import EquipmentManager, EquipmentOperator

        mine = Q(equipment__in=EquipmentManager.objects.filter(manager=user).values("equipment_id")) | Q(
            equipment__in=EquipmentOperator.objects.filter(operator=user).values("equipment_id"))
        return qs.filter(mine).distinct()
    return qs.none()


def _reading(r: UpsReading | None, device: UpsDevice) -> dict | None:
    if r is None:
        return None
    fields = ("reachable", "input_voltage", "output_voltage", "output_frequency", "load_percent", "battery_percent",
              "runtime_minutes", "battery_voltage", "temperature_c", "on_battery", "on_bypass", "low_battery",
              "overload", "fault", "replace_battery", "error")
    out = {f: getattr(r, f) for f in fields}
    out["read_at"] = r.read_at.isoformat()
    out["stale"] = r.read_at < timezone.now() - service.SILENT_AFTER
    out["alerts"] = [{"rule": k, "severity": v[0], "title": v[1]} for k, v in service.conditions(device, r).items()]
    return out


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ups_list(request):
    """UPS units the user looks after (Main Admin: all; Department Admin: their department; OIC / Lab Operator: those
    powering their instruments) with the latest reading and active conditions."""
    service.check_silent()
    rows = []
    for d in _visible_devices(request.user):
        rows.append({
            "id": d.pk, "name": d.name, "manufacturer": d.manufacturer, "model": d.model, "host": d.host,
            "department": d.department.name if d.department_id else "",
            "equipment": [{"id": e.pk, "name": e.name, "code": e.code} for e in d.equipment.all()],
            "map_verified": d.map_verified, "enabled": d.enabled,
            "latest": _reading(service.latest(d), d),
            "health": _health_brief(d),
        })
    return Response({"results": rows})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ups_history(request, device_id):
    """?hours=24 (max 720): 5-minute history for charts."""
    device = _visible_devices(request.user).filter(pk=device_id).first()
    if device is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        hours = max(1, min(720, int(request.query_params.get("hours") or 24)))
    except ValueError:
        hours = 24
    since = timezone.now() - timedelta(hours=hours)
    rows = device.readings.filter(read_at__gte=since).order_by("read_at").values(
        "read_at", "reachable", "load_percent", "battery_percent", "input_voltage", "output_voltage", "on_battery")
    return Response({"device": device.pk, "hours": hours,
                     "points": [{**r, "read_at": r["read_at"].isoformat()} for r in rows]})


def _assessment(a) -> dict | None:
    if a is None:
        return None
    return {"id": a.pk, "created_at": a.created_at.isoformat(), "level": a.level, "risk_score": a.risk_score,
            "findings": a.findings, "metrics": a.metrics, "summary": a.summary, "summary_source": a.summary_source,
            "notified_at": a.notified_at.isoformat() if a.notified_at else None, "notified_codes": a.notified_codes}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ups_health(request, device_id):
    """The latest AI health check of a UPS, the notices sent to the officers and the recent power cuts."""
    from .models import UpsHealthAssessment, UpsPowerEvent
    from .notify import recipients

    device = _visible_devices(request.user).filter(pk=device_id).first()
    if device is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    rows = UpsHealthAssessment.objects.filter(device=device)
    notices = rows.filter(notified_at__isnull=False).order_by("-notified_at")[:10]
    events = UpsPowerEvent.objects.filter(device=device).order_by("-started_at")[:20]
    return Response({
        "device": device.pk,
        "latest": _assessment(rows.order_by("-created_at").first()),
        "notices": [{"at": n.notified_at.isoformat(), "level": n.level, "codes": n.notified_codes,
                     "summary": n.summary, "recipients": len(n.notified_to)} for n in notices],
        "officers": [u.get_display_name() if hasattr(u, "get_display_name") else u.email for u in recipients(device)],
        "notify_officers": device.notify_officers and device.map_verified,
        "power_events": [{"kind": e.kind, "started_at": e.started_at.isoformat(),
                          "ended_at": e.ended_at.isoformat() if e.ended_at else None, "minutes": round(e.minutes, 1),
                          "start_battery_percent": e.start_battery_percent,
                          "end_battery_percent": e.end_battery_percent} for e in events],
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ups_health_run(request, device_id):
    """Run the health check now (shown to the user; officers are told only as the hourly check would)."""
    from .health import run
    from .models import UpsHealthAssessment

    device = _visible_devices(request.user).filter(pk=device_id).first()
    if device is None:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    last = UpsHealthAssessment.objects.filter(device=device).order_by("-created_at").first()
    if last is not None and timezone.now() - last.created_at < timedelta(minutes=2):
        return Response(_assessment(last))
    return Response(_assessment(run(device)))


def _health_brief(device) -> dict | None:
    a = device.assessments.order_by("-created_at").only("level", "risk_score", "summary", "created_at").first()
    if a is None:
        return None
    return {"level": a.level, "risk_score": a.risk_score, "summary": a.summary, "created_at": a.created_at.isoformat()}
