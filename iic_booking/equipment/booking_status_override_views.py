"""API for the Main Administrator's booking status override (see ``booking_status_override``)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import booking_status_override as override
from .models import Booking


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_status_override(request, booking_id):
    """GET: the statuses the Main Administrator may set on this booking. POST {status, reason, notify_user}: set one."""
    if not override.is_main_admin(request.user):
        return Response(
            {"error": "Only the Main Administrator can override a booking status."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if request.method == "GET":
        booking = Booking.objects.filter(pk=booking_id).first()
        if booking is None:
            return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
        return Response(override.options_for(booking))

    data = request.data or {}
    notify = data.get("notify_user", True)
    notify = notify if isinstance(notify, bool) else str(notify).strip().lower() not in ("0", "false", "no")
    try:
        result = override.override_status(
            booking_id,
            target=str(data.get("status") or "").strip().upper(),
            reason=str(data.get("reason") or ""),
            actor=request.user,
            notify_user=notify,
        )
    except override.OverrideRefused as exc:
        return Response({"error": exc.message}, status=exc.http_status)
    return Response(
        {
            "message": (
                f"Status changed to {override.status_label(result.new_status)}."
                + (" The user has been emailed." if result.notified else " No email was sent.")
            ),
            "booking_id": result.booking.booking_id,
            "previous_status": result.previous_status,
            "status": result.new_status,
            "event_id": result.event_id,
            "user_notified": result.notified,
        }
    )
