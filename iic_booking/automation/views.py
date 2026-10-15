"""AI and automation API: instrument finder, best slots, ticket triage, admin switches, dry runs and the action log."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, throttle_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.department_modules.access import is_main_admin
from iic_booking.research_copilot.throttles import ResearchCopilotReadThrottle

from .audit import llm_calls_today, log_action, settings_row
from .models import AutomationActionLog, AutomationSettings, TicketTriage

SETTING_FIELDS = (
    "instrument_finder_enabled",
    "instrument_finder_llm_enabled",
    "llm_daily_call_limit",
    "smart_slots_enabled",
    "ticket_triage_enabled",
    "ticket_auto_assign_enabled",
    "oic_alerts_enabled",
    "oic_alert_utilisation_drop_pct",
    "oic_alert_min_cancellations",
    "oic_alert_min_disruptions",
    "no_show_prompts_enabled",
    "no_show_grace_minutes",
)
_INT_LIMITS = {
    "llm_daily_call_limit": (0, 5000),
    "oic_alert_utilisation_drop_pct": (10, 100),
    "oic_alert_min_cancellations": (1, 50),
    "oic_alert_min_disruptions": (1, 50),
    "no_show_grace_minutes": (0, 720),
}


def _forbidden():
    return Response({"detail": "Only the Main Administrator can do this."}, status=status.HTTP_403_FORBIDDEN)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def features(request):
    cfg = settings_row()
    return Response({"instrument_finder": cfg.instrument_finder_enabled, "smart_slots": cfg.smart_slots_enabled})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotReadThrottle])
def instrument_finder(request):
    from .instrument_finder import MAX_QUERY_CHARS, find_instruments

    cfg = settings_row()
    if not cfg.instrument_finder_enabled:
        return Response({"enabled": False, "results": [], "techniques": []})
    q = (request.query_params.get("q") or "").strip()[:MAX_QUERY_CHARS]
    if len(q) < 3:
        return Response({"enabled": True, "query": q, "results": [], "techniques": [], "tips": [
            "Describe your sample and what you want to find out, for example \u201ccrystal structure of my thin film\u201d."
        ]})
    data = find_instruments(request.user, q, use_llm=bool(cfg.instrument_finder_llm_enabled))
    return Response({"enabled": True, **data})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
@throttle_classes([ResearchCopilotReadThrottle])
def slot_suggestions(request):
    from iic_booking.equipment.models import Equipment

    from .slot_suggestions import suggest

    if not settings_row().smart_slots_enabled:
        return Response({"enabled": False, "suggestions": [], "alternatives": []})
    try:
        eq_id = int(request.query_params.get("equipment_id") or 0)
        slot_count = int(request.query_params.get("slot_count") or 1)
    except (TypeError, ValueError):
        return Response({"detail": "equipment_id and slot_count must be numbers."}, status=status.HTTP_400_BAD_REQUEST)
    eq = Equipment.objects.filter(pk=eq_id).first()
    if eq is None:
        return Response({"detail": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    with_alt = (request.query_params.get("alternatives") or "1") != "0"
    return Response({"enabled": True, **suggest(request.user, eq, slot_count, with_alternatives=with_alt)})


def _settings_payload(cfg: AutomationSettings) -> dict:
    from iic_booking.research_copilot.services.llm_gateway import configured_provider_name

    data = {f: getattr(cfg, f) for f in SETTING_FIELDS}
    data["updated_at"] = cfg.updated_at.isoformat() if cfg.updated_at else None
    data["updated_by"] = cfg.updated_by.get_display_name() if cfg.updated_by_id else None
    data["llm_calls_today"] = llm_calls_today()
    try:
        data["llm_provider"] = configured_provider_name()
    except Exception:  # noqa: BLE001
        data["llm_provider"] = ""
    return data


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def automation_settings(request):
    if not is_main_admin(request.user):
        return _forbidden()
    cfg = AutomationSettings.get_singleton()
    if request.method == "GET":
        return Response(_settings_payload(cfg))
    changes = {}
    for f in SETTING_FIELDS:
        if f not in request.data:
            continue
        value = request.data[f]
        if f in _INT_LIMITS:
            lo, hi = _INT_LIMITS[f]
            try:
                value = int(value)
            except (TypeError, ValueError):
                return Response({"detail": f"{f} must be a whole number."}, status=status.HTTP_400_BAD_REQUEST)
            if not lo <= value <= hi:
                return Response({"detail": f"{f} must be between {lo} and {hi}."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            value = value is True or str(value).lower() in ("true", "1", "yes", "on")
        if getattr(cfg, f) != value:
            changes[f] = [getattr(cfg, f), value]
            setattr(cfg, f, value)
    if changes:
        cfg.updated_by = request.user
        cfg.save()
        log_action("settings", "changed", target_type="settings", target_id="1", actor=request.user,
                   summary=", ".join(changes), detail={k: [str(a), str(b)] for k, (a, b) in changes.items()})
    return Response(_settings_payload(cfg))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def automation_preview(request, kind: str):
    """Dry run: what the automation would do now. Never sends or changes anything."""
    if not is_main_admin(request.user):
        return _forbidden()
    if kind == "oic-alerts":
        from .oic_alerts import run

        return Response(run(dry_run=True))
    if kind == "no-show":
        from .no_show import run

        return Response(run(dry_run=True))
    if kind == "ticket-triage":
        from iic_booking.support.models import Ticket

        from .ticket_triage import triage

        try:
            limit = max(1, min(int(request.query_params.get("limit") or 20), 100))
        except ValueError:
            limit = 20
        rows = []
        for t in Ticket.objects.select_related("related_equipment", "user").order_by("-created_at")[:limit]:
            r = triage(t).as_dict()
            rows.append({"ticket_id": t.ticket_id, "subject": t.subject[:120], "current_type": t.ticket_type,
                         "assigned": bool(t.assigned_to_id), **r})
        return Response({"dry_run": True, "count": len(rows), "results": rows})
    return Response({"detail": "Unknown automation."}, status=status.HTTP_404_NOT_FOUND)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def automation_log(request):
    if not is_main_admin(request.user):
        return _forbidden()
    qs = AutomationActionLog.objects.select_related("recipient", "actor")
    kind = (request.query_params.get("kind") or "").strip()
    if kind:
        qs = qs.filter(kind=kind)
    try:
        limit = max(1, min(int(request.query_params.get("limit") or 100), 500))
    except ValueError:
        limit = 100
    rows = list(qs[:limit])
    return Response({
        "count": len(rows),
        "results": [
            {
                "id": r.pk,
                "kind": r.kind,
                "kind_label": r.get_kind_display(),
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "recipient": r.recipient.get_display_name() if r.recipient_id else None,
                "actor": r.actor.get_display_name() if r.actor_id else None,
                "summary": r.summary,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ],
    })


def _ticket_staff(user, ticket) -> bool:
    from iic_booking.support.ticket_service import user_can_manage_tickets, user_is_ticket_handler

    if user_can_manage_tickets(user):
        from iic_booking.support.ticket_service import user_can_access_ticket

        return user_can_access_ticket(user, ticket)
    return user_is_ticket_handler(user, ticket)


def _triage_payload(row: TicketTriage | None, live=None) -> dict:
    if row is None:
        return {"saved": False, "status": "preview", **(live.as_dict() if live else {})}
    who = row.suggested_assignee
    eq = row.equipment
    return {
        "saved": True,
        "status": row.status,
        "topic": row.topic,
        "category": row.category,
        "priority": row.priority,
        "route_role": row.route_role,
        "equipment": {"equipment_id": eq.pk, "name": eq.name, "code": eq.code or ""} if eq else None,
        "suggested_assignee": {"id": who.pk, "name": who.get_display_name()} if who else None,
        "confidence": row.confidence,
        "reasons": row.reasons,
        "reply_draft": row.reply_draft,
        "draft_sources": row.draft_sources,
        "decided_by": row.decided_by.get_display_name() if row.decided_by_id else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def ticket_triage(request, ticket_id: int):
    from iic_booking.support.models import Ticket

    ticket = get_object_or_404(Ticket.objects.select_related("related_equipment", "user"), pk=ticket_id)
    if not _ticket_staff(request.user, ticket):
        return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
    if not settings_row().ticket_triage_enabled:
        return Response({"enabled": False})
    row = TicketTriage.objects.select_related("suggested_assignee", "equipment", "decided_by").filter(ticket=ticket).first()
    if row is None:
        from .ticket_triage import triage

        return Response({"enabled": True, **_triage_payload(None, triage(ticket))})
    return Response({"enabled": True, **_triage_payload(row)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ticket_triage_decision(request, ticket_id: int):
    """Body: {"accept": true|false, "apply_type": bool, "apply_priority": bool, "assign": bool}. Never sends the draft."""
    from iic_booking.support.models import Ticket
    from iic_booking.support.ticket_service import user_can_manage_tickets

    from .ticket_triage import apply_decision, save_triage

    ticket = get_object_or_404(Ticket.objects.select_related("related_equipment", "user"), pk=ticket_id)
    if not _ticket_staff(request.user, ticket):
        return Response({"detail": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
    if not settings_row().ticket_triage_enabled:
        return Response({"detail": "Ticket triage is switched off."}, status=status.HTTP_409_CONFLICT)
    row = TicketTriage.objects.filter(ticket=ticket).first()
    if row is None:
        _, row = save_triage(ticket, actor=request.user)
    accept = bool(request.data.get("accept"))
    can_assign = user_can_manage_tickets(request.user)
    apply_decision(
        row,
        user=request.user,
        accept=accept,
        apply_type=bool(request.data.get("apply_type", True)),
        apply_priority=bool(request.data.get("apply_priority", True)),
        assign=bool(request.data.get("assign", True)) and can_assign,
    )
    row = TicketTriage.objects.select_related("suggested_assignee", "equipment", "decided_by").get(pk=row.pk)
    return Response({"enabled": True, **_triage_payload(row)})
