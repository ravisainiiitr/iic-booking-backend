"""API for settling bookings on hold for equipment maintenance (see ``maintenance_settlement``)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import maintenance_settlement as settlement
from .booking_status_override import OverrideRefused, status_label
from .models import Equipment

_FORBIDDEN = "Only the Officer In-Charge of this equipment or the Main Administrator can settle its bookings."


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_maintenance_settlement(request, pk):
    """GET: bookings of this equipment on hold for maintenance (no refund taken) and the statuses they may get."""
    equipment = Equipment.objects.filter(pk=pk).first()
    if equipment is None:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if not settlement.may_settle(request.user, equipment.pk):
        return Response({"error": _FORBIDDEN}, status=status.HTTP_403_FORBIDDEN)
    return Response(settlement.overview(equipment))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_maintenance_settle(request, booking_id):
    """POST {status, reason, notify_user}: close a booking on hold for maintenance; it can no longer be refunded."""
    data = request.data or {}
    notify = data.get("notify_user", True)
    notify = notify if isinstance(notify, bool) else str(notify).strip().lower() not in ("0", "false", "no")
    try:
        result = settlement.settle_booking(
            booking_id,
            target=str(data.get("status") or "").strip().upper(),
            reason=str(data.get("reason") or ""),
            actor=request.user,
            notify_user=notify,
        )
    except OverrideRefused as exc:
        return Response({"error": exc.message}, status=exc.http_status)
    return Response(
        {
            "message": (
                f"Booking settled as {status_label(result.new_status)}; it can no longer be refunded."
                + (" The user has been emailed." if result.notified else " No email was sent.")
            ),
            "booking_id": result.booking.booking_id,
            "previous_status": result.previous_status,
            "status": result.new_status,
            "event_id": result.event_id,
            "user_notified": result.notified,
            "refund_locked": True,
        }
    )
