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
