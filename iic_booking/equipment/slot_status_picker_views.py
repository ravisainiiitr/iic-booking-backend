"""
Change Slot Status picker (dashboard menu): GET /api/equipments/slot-status-picker/.

Lists the equipment whose slots the user may change, with the same department / equipment filters as
the waitlist, repeat sample and urgent request lists. Main Administrator: every equipment (default
department IIC is chosen on the page). OIC: equipment they manage, including current temporary OIC
assignments. Everyone else gets 403, matching the slot status and repeat block endpoints.
"""

from __future__ import annotations

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import EquipmentStatus, EquipmentTemporaryOIC
from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
from iic_booking.equipment.staff_list_filters import allowed_equipment_queryset, resolve_staff_list_filter
from iic_booking.users.models.user_type import UserType


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def slot_status_picker(request):
    user = request.user
    user_type = getattr(user, "user_type", None)
    if user_type not in (UserType.ADMIN, UserType.MANAGER):
        return Response(
            {"error": "Only the Main Administrator or an Officer In-charge can change slot status."},
            status=status.HTTP_403_FORBIDDEN,
        )

    allowed_ids = None if user_type == UserType.ADMIN else get_equipment_ids_managed_by_oic(user.id)
    allowed = allowed_equipment_queryset(allowed_ids).exclude(status=EquipmentStatus.DISPOSED)
    list_filter = resolve_staff_list_filter(request, allowed)

    temporary_ids = set()
    if user_type == UserType.MANAGER:
        temporary_ids = set(
            EquipmentTemporaryOIC.objects.filter(temporary_oic=user, resume_at__gt=timezone.now()).values_list(
                "equipment_id", flat=True
            )
        )

    status_labels = dict(EquipmentStatus.choices)
    rows = [
        {
            "equipment_id": row["equipment_id"],
            "code": row["code"] or "",
            "name": row["name"] or row["code"] or "",
            "status": row["status"] or "",
            "status_display": str(status_labels.get(row["status"], row["status"] or "")),
            "department_id": row["internal_department_id"],
            "department_name": row["internal_department__name"] or "",
            "department_code": row["internal_department__code"] or "",
            "temporary_oic": row["equipment_id"] in temporary_ids,
        }
        for row in list_filter.scoped_equipment.order_by("name", "code").values(
            "equipment_id",
            "code",
            "name",
            "status",
            "internal_department_id",
            "internal_department__name",
            "internal_department__code",
        )
    ]
    return Response({"equipment": rows, "count": len(rows), "filters": list_filter.payload})
