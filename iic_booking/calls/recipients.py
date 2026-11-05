"""Who a user may call about a booking, an instrument or an invoice: always the staff configured for that equipment
(``EquipmentOperator``, ``EquipmentManager``; a mode without its own staff uses its base equipment's) or, for accounts,
the active Accounts In Charge (Finance users) of the equipment's department. Never staff of another department, and
never a name typed by the client: the callee is always one of these candidates.

Fallbacks: no Lab Operator reachable -> the Officer In Charge; no Accounts In Charge reachable -> the Officer In Charge
(facility contact); nobody -> a support ticket (``fallback: "ticket"``).

The other way round: the Lab Operator or Officer In Charge of a booking's equipment may call the user of that booking
(role ``user``), and nobody else may. Staff of the equipment only, whatever else the caller can see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.core.cache import cache

from .models import CallContext, CallRole

PRESENCE_KEY = "calls:presence:{}"
PRESENCE_TTL = 90  # seconds; refreshed by the calls WebSocket heartbeat


class CallNotAllowed(Exception):
    def __init__(self, code: str, message: str, status: int = 403):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def calls_open_to(user) -> bool:
    from . import conf

    if not conf.enabled() or not getattr(user, "is_authenticated", False):
        return False
    return not conf.test_accounts_only() or bool(getattr(user, "is_test_account", False))


def is_online(user_id: int) -> bool:
    return bool(cache.get(PRESENCE_KEY.format(user_id)))


def reachable(user) -> bool:
    """Online in a portal tab/app, or has an app install that can be woken by push."""
    from .models import CallDevice

    return is_online(user.pk) or CallDevice.objects.filter(user=user, revoked_at__isnull=True).exists()


@dataclass
class Target:
    context_type: str
    context_id: int
    label: str
    equipment: Any
    roles: tuple[str, ...]
    candidates: dict[str, list] = field(default_factory=dict)


def _equipment_staff(equipment) -> dict[str, list]:
    from django.contrib.auth import get_user_model

    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator
    from iic_booking.users.models.user_type import UserType

    ids = [equipment.pk] + ([equipment.parent_equipment_id] if getattr(equipment, "parent_equipment_id", None) else [])

    def links(model, field_name):
        order = ("role", "pk") if model is EquipmentOperator else ("pk",)  # primary operator before secondary
        rows = list(model.objects.filter(equipment_id__in=ids, **{f"{field_name}__is_active": True})
                    .select_related(field_name).order_by(*order))
        own = [getattr(r, field_name) for r in rows if r.equipment_id == equipment.pk]
        return own or [getattr(r, field_name) for r in rows]

    accounts = []
    if equipment.internal_department_id:
        accounts = list(get_user_model().objects.filter(user_type=UserType.FINANCE, is_active=True,
                                                        department_id=equipment.internal_department_id).order_by("pk"))

    def unique(users):
        seen, out = set(), []
        for u in users:
            if u.pk not in seen:
                seen.add(u.pk)
                out.append(u)
        return out

    return {
        CallRole.OPERATOR: unique(links(EquipmentOperator, "operator")),
        CallRole.OIC: unique(links(EquipmentManager, "manager")),
        CallRole.ACCOUNTS: unique(accounts),
    }


def resolve_target(user, context_type: str, context_id: int) -> Target:
    """The booking / equipment the user may ask about, or CallNotAllowed."""
    from iic_booking.equipment.api_views import bookings_visible_to
    from iic_booking.equipment.global_search import _searchable_equipment_ids
    from iic_booking.equipment.models import Equipment

    booking = None
    if context_type in (CallContext.BOOKING, CallContext.INVOICE):
        booking = bookings_visible_to(user).select_related("equipment", "user").filter(pk=context_id).first()
        if booking is None:
            raise CallNotAllowed("not_found", "Booking not found.", 404)
        ref = booking.virtual_booking_id or f"{booking.equipment.code}-{booking.pk}"
        roles = ((CallRole.ACCOUNTS,) if context_type == CallContext.INVOICE
                 else (CallRole.OPERATOR, CallRole.OIC, CallRole.ACCOUNTS))
        target = Target(context_type, booking.pk, f"Booking {ref}", booking.equipment, roles)
    elif context_type == CallContext.EQUIPMENT:
        if int(context_id) not in _searchable_equipment_ids(user):
            raise CallNotAllowed("not_found", "Equipment not found.", 404)
        equipment = Equipment.objects.get(pk=context_id)
        target = Target(context_type, equipment.pk, equipment.name, equipment, (CallRole.OPERATOR, CallRole.OIC))
    else:
        raise CallNotAllowed("invalid", "Unknown call context.", 400)
    from . import conf

    staff = _equipment_staff(target.equipment)
    if context_type == CallContext.BOOKING and any(p.pk == user.pk for p in staff[CallRole.OPERATOR] + staff[CallRole.OIC]):
        target.roles += (CallRole.USER,)
        staff[CallRole.USER] = [booking.user] if booking.user.is_active else []
    for role in CallRole.values:  # all roles: a fallback may point to one the context does not offer
        people = [p for p in staff.get(role, []) if p.pk != user.pk]
        if conf.test_accounts_only():
            people = [p for p in people if getattr(p, "is_test_account", False)]
        target.candidates[role] = people
    return target


def options(user, context_type: str, context_id: int) -> dict[str, Any]:
    """Call choices for the UI: per role the people (with availability), the fallback and why."""
    from iic_booking.users.display import get_user_display_name

    target = resolve_target(user, context_type, context_id)
    roles = []
    for role in target.roles:
        people = [{"id": p.pk, "name": get_user_display_name(p), "available": reachable(p)}
                  for p in target.candidates.get(role, [])]
        available = [p for p in people if p["available"]]
        fallback = ""
        if not available and role != CallRole.USER:  # staff calling a user have no one to fall back to
            other = CallRole.OIC if role in (CallRole.OPERATOR, CallRole.ACCOUNTS) else ""
            fallback = "oic" if other and any(reachable(p) for p in target.candidates.get(other, [])) else "ticket"
        roles.append({"role": role, "label": CallRole(role).label, "people": people, "fallback": fallback})
    return {"context_type": target.context_type, "context_id": target.context_id, "label": target.label, "roles": roles}
