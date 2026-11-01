"""Responsible people for equipment: Lab Operator(s), Officer(s) in Charge and the department's Accounts In Charge.

Shown with equipment and booking search results (Module 2). Built for many equipment at once in a fixed number of
queries. The same people the booking details already name (EquipmentOperator / EquipmentManager links; Accounts In
Charge = active Finance users of the equipment's department). A mode with no staff of its own uses its base
equipment's. Only work contact details are given: name, designation, the account email and, for signed-in viewers,
the phone number on the staff profile.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable


def _person(user, honorific: str, role: str, *, with_phone: bool) -> dict[str, Any]:
    from iic_booking.users.display import get_user_display_name, name_with_honorific

    return {
        "name": name_with_honorific(user, honorific, default=get_user_display_name(user)),
        "designation": (getattr(user, "designation", "") or "").strip() or role,
        "email": user.email or "",
        "phone": (user.phone_number or "").strip() if with_phone else "",
    }


def contacts_for_equipment(equipment_list: Iterable, *, with_phone: bool) -> dict[int, dict[str, list[dict]]]:
    """``{equipment_id: {"lab_operators": [...], "oics": [...], "accounts_in_charge": [...]}}``."""
    from django.contrib.auth import get_user_model

    from iic_booking.users.models.user_type import UserType

    from .models import EquipmentManager, EquipmentOperator

    equipment_list = [e for e in equipment_list if e is not None]
    if not equipment_list:
        return {}
    own = {e.equipment_id: e for e in equipment_list}
    base_of = {e.equipment_id: getattr(e, "parent_equipment_id", None) for e in equipment_list}
    staff_ids = set(own) | {b for b in base_of.values() if b}

    operators: dict[int, list] = defaultdict(list)
    for link in (EquipmentOperator.objects.filter(equipment_id__in=staff_ids, operator__is_active=True)
                 .select_related("operator").order_by("equipment_operator_id")):
        operators[link.equipment_id].append(_person(link.operator, link.honorific, "Lab Operator",
                                                    with_phone=with_phone))
    oics: dict[int, list] = defaultdict(list)
    for link in (EquipmentManager.objects.filter(equipment_id__in=staff_ids, manager__is_active=True)
                 .select_related("manager").order_by("equipment_manager_id")):
        oics[link.equipment_id].append(_person(link.manager, link.honorific, "Officer In Charge",
                                               with_phone=with_phone))

    dept_ids = {e.internal_department_id for e in equipment_list if getattr(e, "internal_department_id", None)}
    accounts: dict[int, list] = defaultdict(list)
    if dept_ids:
        for u in (get_user_model().objects.filter(user_type=UserType.FINANCE, is_active=True,
                                                   department_id__in=dept_ids).order_by("pk")):
            accounts[u.department_id].append(_person(u, "", "Accounts In Charge", with_phone=with_phone))

    out = {}
    for eq_id, e in own.items():
        base = base_of.get(eq_id)
        out[eq_id] = {
            "lab_operators": operators.get(eq_id) or operators.get(base, []),
            "oics": oics.get(eq_id) or oics.get(base, []),
            "accounts_in_charge": accounts.get(getattr(e, "internal_department_id", None), []),
        }
    return out
