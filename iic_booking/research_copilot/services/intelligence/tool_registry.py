"""
Copilot tool registry.

Maps each capability to an existing portal service. The chat engine may call read tools and
`prepare_*` tools (which only build a confirmation proposal). Tools that change records are
listed for completeness but have no chat handler: they run only through the explicit confirmation
endpoint (`/mutations/confirm/`) or the explicit "Raise support ticket" endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


class ToolNotAllowed(PermissionError):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    mutating: bool = False
    requires_confirmation: bool = False
    executes_via: str = "chat"


def _search_equipment(*, user, technique_keys, text="", limit=6, offset=0):
    from iic_booking.research_copilot.services.intelligence import equipment

    return equipment.search(user=user, technique_keys=technique_keys, text=text, limit=limit, offset=offset)


def _get_equipment_details(*, user, equipment_id):
    from iic_booking.research_copilot.services.intelligence import equipment

    eq = equipment.get_visible(user, equipment_id)
    return equipment.row(eq) if eq else None


def _check_availability(*, user, equipment_id, start_date, end_date, after_time=None, limit=80):
    from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots

    return find_bookable_slots(
        user=user, equipment_id=equipment_id, start_date=start_date, end_date=end_date, after_time=after_time, limit=limit
    )


def _estimate_cost(*, user, equipment_id, samples=None):
    from iic_booking.research_copilot.services import tools as tools_svc

    args: dict[str, Any] = {"equipment_id": int(equipment_id)}
    if samples:
        args["A"] = int(samples)
    return tools_svc._estimate_booking_cost(arguments=args, user=user)


def _list_user_bookings(*, user):
    from iic_booking.research_copilot.services.intelligence import booking_changes

    return booking_changes.cancellable_bookings(user)


def _prepare_booking(*, user, **kwargs):
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    return booking_mut.prepare_booking_create(user=user, **kwargs)


def _preview_cancellation(*, user, booking_id, body):
    from iic_booking.research_copilot.services.v2.mutations import domain_bridge

    return domain_bridge.call_partial_cancel_preview(user=user, booking_id=int(booking_id), body=body)


def _prepare_cancellation(*, user, **kwargs):
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    return booking_mut.prepare_cancellation(user=user, **kwargs)


def _prepare_reschedule(*, user, **kwargs):
    from iic_booking.research_copilot.services.v2.mutations import booking as booking_mut

    return booking_mut.prepare_reschedule(user=user, **kwargs)


def _search_knowledge(*, user, text, limit=3):
    from iic_booking.research_copilot.services.intelligence import knowledge

    return knowledge.search(text=text, user=user, limit=limit)


TOOLS: dict[str, ToolSpec] = {
    s.name: s
    for s in (
        ToolSpec("search_equipment", "Search visible equipment by technique or name"),
        ToolSpec("get_equipment_details", "Portal fields for one visible instrument"),
        ToolSpec("check_availability", "Bookable slots with the booking-page rules"),
        ToolSpec("estimate_cost", "Charge estimate from the portal charge engine"),
        ToolSpec("list_user_bookings", "The signed-in user's cancellable bookings"),
        ToolSpec("prepare_booking", "Validate and build a booking confirmation proposal", requires_confirmation=True),
        ToolSpec("execute_booking", "Create the booking", True, True, "POST /mutations/confirm/"),
        ToolSpec("preview_cancellation", "Refund and remaining booking for a partial cancellation"),
        ToolSpec("prepare_cancellation", "Build a cancellation confirmation proposal", requires_confirmation=True),
        ToolSpec("execute_cancellation", "Cancel the booking or selected slots", True, True, "POST /mutations/confirm/"),
        ToolSpec("prepare_reschedule", "Build a reschedule confirmation proposal", requires_confirmation=True),
        ToolSpec("execute_reschedule", "Move the booking", True, True, "POST /mutations/confirm/"),
        ToolSpec("search_knowledge", "Approved Copilot knowledge articles"),
        ToolSpec(
            "create_support_ticket",
            "Raise a ticket in the portal support system",
            True,
            True,
            "POST /conversations/<id>/escalate/",
        ),
    )
}

_HANDLERS: dict[str, Callable[..., Any]] = {
    "search_equipment": _search_equipment,
    "get_equipment_details": _get_equipment_details,
    "check_availability": _check_availability,
    "estimate_cost": _estimate_cost,
    "list_user_bookings": _list_user_bookings,
    "prepare_booking": _prepare_booking,
    "preview_cancellation": _preview_cancellation,
    "prepare_cancellation": _prepare_cancellation,
    "prepare_reschedule": _prepare_reschedule,
    "search_knowledge": _search_knowledge,
}


def call(name: str, **kwargs) -> Any:
    spec = TOOLS.get(name)
    if spec is None:
        raise ToolNotAllowed(f"Unknown tool: {name}")
    if spec.mutating or name not in _HANDLERS:
        raise ToolNotAllowed(f"{name} changes portal records and runs only via {spec.executes_via}")
    return _HANDLERS[name](**kwargs)


def describe() -> list[dict[str, Any]]:
    return [
        {
            "name": s.name,
            "description": s.description,
            "mutating": s.mutating,
            "requires_confirmation": s.requires_confirmation,
            "executes_via": s.executes_via,
        }
        for s in TOOLS.values()
    ]
