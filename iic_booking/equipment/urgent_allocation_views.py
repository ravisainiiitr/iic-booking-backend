"""OIC endpoints for Type B urgent requests without slots: slot picker, amount check and Approve & allocate."""

from __future__ import annotations

import logging
from datetime import datetime

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import DailySlot, SlotStatus, UrgentBookingRequest
from .urgent_allocation import (
    UrgentAllocationError,
    allocate_urgent_request,
    holidays_for,
    quote_allocation,
    slot_notes,
)

logger = logging.getLogger(__name__)


def _load_request_for_staff(request, request_id):
    """(urg, error_response) for an urgent request the signed-in staff member may decide."""
    from . import api_views

    if not api_views.check_operator_permission(request.user):
        return None, Response(
            {"error": "Only admin and Officer in charge can allocate urgent requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        urg = UrgentBookingRequest.objects.select_related("user", "equipment", "hold_booking").get(pk=int(request_id))
    except (TypeError, ValueError, UrgentBookingRequest.DoesNotExist):
        return None, Response({"error": "Urgent request not found."}, status=status.HTTP_404_NOT_FOUND)
    allowed_ids = api_views._get_equipment_ids_for_log_access(request.user)
    if allowed_ids is not None and urg.equipment_id not in allowed_ids:
        return None, Response(
            {"error": "You do not have permission to update this request (equipment not under your charge)."},
            status=status.HTTP_403_FORBIDDEN,
        )
    return urg, None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def urgent_allocation_slots(request, request_id):
    """
    Every slot of one date (any status: weekends, holidays, closed, blocked, maintenance) so the OIC can
    allocate a Type B request without slots. Query: date=YYYY-MM-DD.
    """
    from .slot_utils import SlotGenerator

    urg, err = _load_request_for_staff(request, request_id)
    if err:
        return err
    raw_date = (request.query_params.get("date") or "").strip()
    try:
        target_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
    except ValueError:
        return Response({"error": "Provide date as YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
    equipment = urg.equipment
    SlotGenerator.ensure_slot_masters_exist(equipment)
    SlotGenerator.generate_slots_for_week(equipment, target_date, target_date, allow_holiday=True)
    slots = list(
        DailySlot.objects.filter(slot_master__equipment=equipment, date=target_date)
        .select_related("booking", "booking__user")
        .order_by("start_datetime")
    )
    holidays = holidays_for({target_date})
    rows = []
    for s in slots:
        booked = bool(s.booking_id) or s.status == SlotStatus.BOOKED
        rows.append({
            "id": s.id,
            "start_datetime": s.start_datetime.isoformat() if s.start_datetime else None,
            "end_datetime": s.end_datetime.isoformat() if s.end_datetime else None,
            "status": s.status,
            "status_display": s.get_status_display(),
            "selectable": not booked,
            "notes": [n for n in slot_notes(s, holidays) if not booked],
            "booked_by": (
                getattr(getattr(s.booking, "user", None), "name", None)
                or getattr(getattr(s.booking, "user", None), "email", None)
            ) if s.booking_id else None,
        })
    return Response({
        "date": target_date.isoformat(),
        "is_weekend": target_date.weekday() >= 5,
        "holiday": holidays.get(target_date) if target_date in holidays else None,
        "slot_duration_minutes": int(getattr(equipment, "slot_duration_minutes", None) or 60),
        "required_minutes": urg.duration_minutes,
        "required_slots": urg.slots_requested,
        "slots": rows,
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def urgent_allocation_quote(request, request_id):
    """Amount for the chosen slots, worked out now, and whether the requester's wallet can pay it. Body: slot_ids."""
    urg, err = _load_request_for_staff(request, request_id)
    if err:
        return err
    if not urg.requires_slot_allocation:
        return Response({"error": "This request already has held slots."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        result = quote_allocation(urg, request.data.get("slot_ids") or [])
    except UrgentAllocationError as exc:
        return Response(exc.payload(), status=exc.http_status)
    return Response(result["payload"], status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def urgent_allocate(request, request_id):
    """
    Approve a Type B request without slots by booking the chosen slots for the requester (any day).
    Body: slot_ids, expected_total (amount shown to the OIC), admin_notes (optional).
    The wallet is debited and the usual booking confirmation goes to the user and the supervisor.
    """
    from . import api_views

    urg, err = _load_request_for_staff(request, request_id)
    if err:
        return err
    try:
        urg, booking, payload = allocate_urgent_request(
            urg.id,
            request.user,
            request.data.get("slot_ids") or [],
            admin_notes=request.data.get("admin_notes") if "admin_notes" in request.data else None,
            expected_total=request.data.get("expected_total"),
        )
    except UrgentAllocationError as exc:
        return Response(exc.payload(), status=exc.http_status)
    api_views.record_staff_action(
        request.user,
        "urgent_request.allocate",
        equipment_id=urg.equipment_id,
        urgent_request_id=urg.id,
        booking_id=booking.booking_id,
        slot_ids=payload["slot_ids"],
    )
    try:
        api_views._notify_urgent_request_decided(urg, request.user, requester_already_notified=True)
    except Exception:
        logger.exception("In-app notifications failed for allocated urgent request %s", urg.id)
    return Response(
        {
            "message": "Booking allocated. The user and the supervisor will get the booking confirmation.",
            "id": urg.id,
            "status": urg.status,
            "booking_id": booking.booking_id,
            "booking_display_id": api_views.booking_display_id_for_email(booking),
            "total_charge": payload["total_charge"],
            "slot_times": payload["slot_times"],
        },
        status=status.HTTP_200_OK,
    )
