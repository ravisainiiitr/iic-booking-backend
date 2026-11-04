"""Upkeep API (``/api/v1/upkeep/``): equipment list with uptime, equipment detail, plans, completions, alerts."""

from __future__ import annotations

from functools import wraps

from django.db.models import Count, Min, Q
from django.shortcuts import get_object_or_404
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import service as svc
from .models import AlertKind, MaintenancePlan, PlanKind, ReliabilityAlert

MAX_LIST = 500


def upkeep_api(methods):
    def deco(fn):
        @api_view(methods)
        @permission_classes([IsAuthenticated])
        @wraps(fn)
        def inner(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except svc.UpkeepError as exc:
                return Response({"detail": exc.message, "code": exc.code}, status=exc.status)

        return inner

    return deco


def _iso(v):
    return v.isoformat() if v else None


def _name(user):
    if user is None:
        return None
    from iic_booking.users.display import get_user_display_name

    return {"id": user.pk, "name": get_user_display_name(user)}


def _eq(eq) -> dict:
    return {"equipment_id": eq.pk, "name": eq.name, "code": eq.code, "status": eq.status or ""}


def plan_out(p: MaintenancePlan, user=None) -> dict:
    return {
        "id": p.pk,
        "equipment_id": p.equipment_id,
        "kind": p.kind,
        "kind_label": p.get_kind_display(),
        "title": p.title,
        "instructions": p.instructions,
        "interval_days": p.interval_days,
        "lead_days": p.lead_days,
        "last_done_on": _iso(p.last_done_on),
        "next_due_on": _iso(p.next_due_on),
        "state": svc.plan_state(p),
        "responsible": _name(p.responsible),
        "is_active": p.is_active,
        "closed_reason": p.closed_reason,
        "can_record": bool(user and p.is_active and svc.can_record(user, p)),
    }


def completion_out(c) -> dict:
    return {
        "id": c.pk,
        "plan_id": c.plan_id,
        "plan_title": c.plan.title,
        "done_on": _iso(c.done_on),
        "done_by": _name(c.done_by),
        "notes": c.notes,
        "report_reference": c.report_reference,
        "next_due_on": _iso(c.next_due_on),
    }


def alert_out(a: ReliabilityAlert) -> dict:
    return {
        "id": a.pk,
        "equipment": _eq(a.equipment),
        "kind": a.kind,
        "kind_label": a.get_kind_display(),
        "severity": a.severity,
        "message": a.message,
        "opened_at": _iso(a.opened_at),
        "resolved_at": _iso(a.resolved_at),
        "acknowledged_by": _name(a.acknowledged_by),
        "acknowledged_at": _iso(a.acknowledged_at),
        "acknowledge_note": a.acknowledge_note,
    }


def setting_out(eq) -> dict:
    row = svc.setting_for(eq)
    return {
        "uptime_target_pct": float(row.uptime_target_pct),
        "breakdown_alert_count": row.breakdown_alert_count,
        "breakdown_window_days": row.breakdown_window_days,
    }


def _equipment_qs(user):
    from iic_booking.equipment.models import Equipment

    ids = svc.visible_equipment_ids(user)
    qs = Equipment.objects.all()
    if ids is not None:
        qs = qs.filter(pk__in=ids)
    return qs


@upkeep_api(["GET"])
def equipment_list(request):
    qs = _equipment_qs(request.user)
    q = (request.query_params.get("q") or "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(code__icontains=q))
    rows = list(qs.order_by("name")[:MAX_LIST])
    ids = [e.pk for e in rows]
    m = svc.metrics_many(rows, request.query_params.get("days"))
    alerts = dict(ReliabilityAlert.objects.filter(equipment_id__in=ids, resolved_at__isnull=True)
                  .values("equipment_id").annotate(n=Count("pk")).values_list("equipment_id", "n"))
    due = {r["equipment_id"]: r for r in MaintenancePlan.objects.filter(equipment_id__in=ids, is_active=True)
           .values("equipment_id").annotate(next_due=Min("next_due_on"), plans=Count("pk"))}
    managed = svc.managed_equipment_ids(request.user)
    return Response({
        "results": [
            {
                "equipment": _eq(e),
                "uptime_pct": m[e.pk]["uptime_pct"],
                "target_pct": m[e.pk]["target_pct"],
                "below_target": m[e.pk]["below_target"],
                "breakdowns": m[e.pk]["breakdowns"],
                "downtime_hours": m[e.pk]["downtime_hours"],
                "open_alerts": alerts.get(e.pk, 0),
                "plans": due.get(e.pk, {}).get("plans", 0),
                "next_due_on": _iso(due.get(e.pk, {}).get("next_due")),
                "can_manage": managed is None or e.pk in managed,
            }
            for e in rows
        ],
        "truncated": len(rows) == MAX_LIST,
    })


@upkeep_api(["GET", "PUT"])
def equipment_detail(request, equipment_id: int):
    from iic_booking.equipment.models import Equipment

    eq = get_object_or_404(Equipment, pk=equipment_id)
    svc.require(svc.can_view(request.user, eq.pk))
    if request.method == "PUT":
        svc.save_setting(eq, request.user, request.data)
    plans = MaintenancePlan.objects.filter(equipment=eq).select_related("responsible")
    from .models import PlanCompletion

    completions = PlanCompletion.objects.filter(plan__equipment=eq).select_related("plan", "done_by")[:50]
    alerts = ReliabilityAlert.objects.filter(equipment=eq).select_related("equipment", "acknowledged_by")
    return Response({
        "equipment": _eq(eq),
        "setting": setting_out(eq),
        "metrics": svc.metrics(eq, request.query_params.get("days")),
        "plans": [plan_out(p, request.user) for p in plans.order_by("-is_active", "next_due_on")],
        "completions": [completion_out(c) for c in completions],
        "alerts": [alert_out(a) for a in alerts.filter(resolved_at__isnull=True)]
        + [alert_out(a) for a in alerts.filter(resolved_at__isnull=False)[:20]],
        "can_manage": svc.can_manage(request.user, eq.pk),
        "kinds": [{"value": v, "label": str(lbl)} for v, lbl in PlanKind.choices],
    })


@upkeep_api(["POST"])
def plan_create(request, equipment_id: int):
    from iic_booking.equipment.models import Equipment

    eq = get_object_or_404(Equipment, pk=equipment_id)
    return Response(plan_out(svc.create_plan(eq, request.user, request.data), request.user), status=201)


@upkeep_api(["PATCH"])
def plan_detail(request, pk: int):
    plan = get_object_or_404(MaintenancePlan.objects.select_related("equipment"), pk=pk)
    svc.require(svc.can_view(request.user, plan.equipment_id))
    return Response(plan_out(svc.update_plan(plan, request.user, request.data), request.user))


@upkeep_api(["POST"])
def plan_close(request, pk: int):
    plan = get_object_or_404(MaintenancePlan.objects.select_related("equipment"), pk=pk)
    return Response(plan_out(svc.close_plan(plan, request.user, request.data.get("reason")), request.user))


@upkeep_api(["POST"])
def plan_complete(request, pk: int):
    plan = get_object_or_404(MaintenancePlan.objects.select_related("equipment"), pk=pk)
    row = svc.complete_plan(plan, request.user, request.data)
    plan.refresh_from_db()
    return Response({"completion": completion_out(row), "plan": plan_out(plan, request.user)}, status=201)


@upkeep_api(["GET"])
def alert_list(request):
    ids = svc.visible_equipment_ids(request.user)
    qs = ReliabilityAlert.objects.select_related("equipment", "acknowledged_by")
    if ids is not None:
        qs = qs.filter(equipment_id__in=ids)
    if request.query_params.get("include_resolved") not in ("1", "true"):
        qs = qs.filter(resolved_at__isnull=True)
    kind = request.query_params.get("kind")
    if kind in AlertKind.values:
        qs = qs.filter(kind=kind)
    return Response({"results": [alert_out(a) for a in qs[:300]]})


@upkeep_api(["POST"])
def alert_acknowledge(request, pk: int):
    alert = get_object_or_404(ReliabilityAlert.objects.select_related("equipment"), pk=pk)
    return Response(alert_out(svc.acknowledge(alert, request.user, request.data.get("note") or "")))
