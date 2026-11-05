"""Equipment open only to OIC requests ("Only Officers In-charge can raise requests").

With ``Equipment.oic_requests_only`` on, the only way to book the equipment is an OIC request (see
``oic_request_registration``) raised by an Officer In-charge of any equipment in the same department, in their own
name. The Main Administrator and other staff keep their view of the equipment but cannot raise a request. These OICs
get access to raise requests only: the equipment does not join their managed equipment, so slot and booking management
stay with its own OIC.
"""

from __future__ import annotations

from rest_framework import status
from rest_framework.response import Response

CODE = "OIC_REQUESTS_ONLY"


def is_oic_only(equipment) -> bool:
    return bool(getattr(equipment, "oic_requests_only", False))


def departments_of_oic(user) -> set[int]:
    """Departments of the equipment this OIC manages, as primary or active temporary OIC."""
    from iic_booking.users.models.user_type import UserType

    from .models import Equipment
    from .reports import get_equipment_ids_managed_by_oic

    if not getattr(user, "is_authenticated", False) or getattr(user, "user_type", None) != UserType.MANAGER:
        return set()
    managed = get_equipment_ids_managed_by_oic(user.id)
    if not managed:
        return set()
    return set(
        Equipment.objects.filter(pk__in=managed, internal_department__isnull=False)
        .order_by()
        .values_list("internal_department_id", flat=True)
        .distinct()
    )


def may_raise_request(user, equipment) -> bool:
    dept_id = getattr(equipment, "internal_department_id", None)
    return dept_id is not None and dept_id in departments_of_oic(user)


def _who_may_raise(equipment) -> str:
    dept = getattr(equipment, "internal_department", None)
    name = getattr(dept, "name", "") or "this department"
    return f"Only an Officer In-charge in {name} can raise a request for this equipment."


def raise_refusal(user, equipment) -> str | None:
    """None when ``user`` may raise an OIC request for this equipment, else the reason."""
    return None if may_raise_request(user, equipment) else _who_may_raise(equipment)


def booking_refusal(user, equipment, *, oic_registration: bool) -> str | None:
    """Reason a booking request is refused on OIC-only equipment; None when it may go ahead."""
    if not is_oic_only(equipment):
        return None
    refusal = raise_refusal(user, equipment)
    if refusal:
        return refusal
    return None if oic_registration else "This equipment takes OIC requests only. Register it as an OIC request."


def refused_response(request, equipment, route: str, reason: str | None = None) -> Response:
    """Audit a refused attempt and answer 403."""
    from .oic_request_audit import record_refusal

    reason = reason or _who_may_raise(equipment)
    record_refusal(request, equipment, route=route, reason=reason)
    return Response({"error": reason, "code": CODE}, status=status.HTTP_403_FORBIDDEN)
