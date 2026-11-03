"""API: whether the OIC may register requests for an equipment, and the requests they registered."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import Equipment
from .oic_request_registration import oic_registration_refusal, registered_requests


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_registered_requests(request, pk: int):
    equipment = Equipment.objects.filter(pk=pk).first()
    if equipment is None:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    refusal = oic_registration_refusal(request.user, equipment)
    if refusal:
        return Response({"allowed": False, "error": refusal, "results": []}, status=status.HTTP_403_FORBIDDEN)
    rows = []
    for booking in registered_requests(request.user, equipment)[:100]:
        slots = sorted(booking.daily_slots.all(), key=lambda s: s.start_datetime)
        rows.append(
            {
                "booking_id": booking.booking_id,
                "reference": booking.virtual_booking_id or str(booking.booking_id),
                "status": booking.status,
                "status_display": booking.get_status_display(),
                "start_time": slots[0].start_datetime.isoformat() if slots else None,
                "end_time": slots[-1].end_datetime.isoformat() if slots else None,
                "notes": booking.notes or "",
                "created_at": booking.created_at.isoformat(),
            }
        )
    return Response({"allowed": True, "equipment_id": equipment.pk, "results": rows})
