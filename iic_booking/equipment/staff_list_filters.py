"""
Department and equipment filters for staff lists (equipment waitlist, repeat samples, urgent requests).

The caller passes the equipment the signed-in user may see. ``department_id`` and ``equipment_id``
query params only narrow that set; they never widen it, so an OIC asking for another department or
someone else's equipment simply gets no rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from django.db.models import QuerySet

from iic_booking.users.models.user_type import UserType


def _positive_int_param(request, name: str) -> Optional[int]:
    raw = (request.query_params.get(name) or "").strip()
    if not raw or raw.lower() == "all":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def staff_list_scope(user) -> str:
    """``all`` (Main Admin), ``department`` (Department Administrator) or ``equipment`` (OIC, Lab Operator)."""
    ut = getattr(user, "user_type", None)
    if ut == UserType.ADMIN:
        return "all"
    if ut == UserType.DEPT_ADMIN:
        return "department"
    return "equipment"


def allowed_equipment_queryset(allowed_ids: Optional[list[int]]) -> QuerySet:
    from iic_booking.equipment.models import Equipment

    qs = Equipment.objects.all()
    if allowed_ids is not None:
        qs = qs.filter(equipment_id__in=list(allowed_ids))
    return qs


@dataclass
class StaffListFilter:
    department_id: Optional[int]
    equipment_id: Optional[int]
    scoped_equipment: QuerySet
    unrestricted: bool
    payload: dict[str, Any]

    def apply(self, queryset: QuerySet, field: str = "equipment_id") -> QuerySet:
        """Limit ``queryset`` (rows with an equipment FK reachable through ``field``) to the filtered equipment."""
        if self.unrestricted:
            return queryset
        return queryset.filter(**{f"{field}__in": self.scoped_equipment.values("equipment_id")})


def resolve_staff_list_filter(request, allowed_equipment: QuerySet, *, unrestricted: bool = False) -> StaffListFilter:
    """
    ``allowed_equipment``: Equipment queryset the user may see. Pass ``unrestricted=True`` only when that
    queryset is every equipment (Main Admin), so an unfiltered list skips the equipment subquery.
    """
    user = request.user
    department_id = _positive_int_param(request, "department_id")
    equipment_id = _positive_int_param(request, "equipment_id")

    in_department = allowed_equipment
    if department_id is not None:
        in_department = in_department.filter(internal_department_id=department_id)
    scoped = in_department
    if equipment_id is not None:
        scoped = scoped.filter(equipment_id=equipment_id)

    options = [
        {
            "equipment_id": row["equipment_id"],
            "code": row["code"] or "",
            "name": row["name"] or row["code"] or "",
            "department_id": row["internal_department_id"],
        }
        for row in in_department.order_by("name", "code").values(
            "equipment_id", "code", "name", "internal_department_id"
        )
    ]
    scope = staff_list_scope(user)
    payload = {
        "scope": scope,
        "department_locked": scope != "all",
        "locked_department_id": getattr(user, "department_id", None) if scope == "department" else None,
        "department_id": department_id,
        "equipment_id": equipment_id,
        "equipment_options": options,
    }
    return StaffListFilter(
        department_id=department_id,
        equipment_id=equipment_id,
        scoped_equipment=scoped,
        unrestricted=unrestricted and department_id is None and equipment_id is None,
        payload=payload,
    )
