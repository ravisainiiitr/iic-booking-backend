"""Slot Exchange API (see ``slot_exchange`` for the rules).

GET  slot-exchanges/                     the viewer's requests (incoming and outgoing)
POST slot-exchanges/                     {offered_slot_ids, requested_slot_ids, message}
GET  slot-exchanges/options/?slot_id=N   who booked slot N and which of the viewer's slots could be given for it
POST slot-exchanges/<id>/accept|decline|cancel/
"""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from . import slot_exchange as sx
from .models import Booking, BookingStatus, DailySlot
from .slot_exchange_models import SlotExchangeRequest, SlotExchangeStatus


def _error(exc: sx.ExchangeError, http_status=status.HTTP_400_BAD_REQUEST):
    return Response({"error": exc.message, "code": exc.code}, status=http_status)


def _ids(raw) -> list[int]:
    if not isinstance(raw, (list, tuple)):
        raise sx.ExchangeError("Slot ids must be a list.", "BAD_INPUT")
    try:
        return [int(v) for v in raw]
    except (TypeError, ValueError):
        raise sx.ExchangeError("Slot ids must be numbers.", "BAD_INPUT") from None


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def slot_exchanges(request):
    user = request.user
    if request.method == "POST":
        try:
            req = sx.create_request(
                user,
                _ids(request.data.get("offered_slot_ids")),
                _ids(request.data.get("requested_slot_ids")),
                request.data.get("message") or "",
            )
        except sx.ExchangeError as exc:
            return _error(exc)
        return Response(sx.serialize_request(req, user), status=status.HTTP_201_CREATED)

    mine = SlotExchangeRequest.objects.filter(Q(requester=user) | Q(responder=user))
    sx.expire_stale(mine)
    rows = mine.select_related(
        "requester", "requester__department", "responder", "responder__department",
        "requester_booking", "responder_booking", "responder_booking__equipment",
    )[:200]
    data = [sx.serialize_request(r, user) for r in rows]
    return Response(
        {
            "results": data,
            "pending_incoming": sum(1 for r in data if r["can_respond"]),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def slot_exchange_options(request):
    """For a booked slot on a calendar: the booker and the exchange choices open to the viewer."""
    user = request.user
    try:
        slot_id = int(request.query_params.get("slot_id"))
    except (TypeError, ValueError):
        return Response({"error": "slot_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    slot = DailySlot.objects.select_related("slot_master__equipment", "booking__user__department").filter(
        pk=slot_id
    ).first()
    if slot is None or slot.booking_id is None:
        return Response({"error": "This slot is not booked."}, status=status.HTTP_404_NOT_FOUND)
    booking = slot.booking
    equipment = slot.slot_master.equipment
    if not sx.is_internal_user(user) or not sx.booking_is_internal(booking):
        return Response({"error": "Booker details are shown only between internal users."},
                        status=status.HTTP_403_FORBIDDEN)

    now = timezone.now()
    theirs = list(booking.daily_slots.order_by("start_datetime"))
    payload = {
        "slot_id": slot.pk,
        "booking_id": booking.pk,
        "booking_ref": booking.virtual_booking_id or f"{equipment.code}-#{booking.pk}",
        "booker": sx.booker_details(booking.user),
        "is_own_booking": booking.user_id == user.pk,
        "equipment": {"id": equipment.pk, "name": equipment.name, "code": equipment.code,
                      "slot_exchange_enabled": bool(equipment.slot_exchange_enabled)},
        "their_slots": [sx.slot_summary(s) for s in theirs],
        "their_per_slot_charge": str(sx.per_slot_charge(booking, len(theirs))),
        "exchange_available": False,
        "exchange_unavailable_reason": None,
        "my_bookings": [],
    }
    if payload["is_own_booking"]:
        payload["exchange_unavailable_reason"] = "This is your own booking."
        return Response(payload)
    if not equipment.slot_exchange_enabled:
        payload["exchange_unavailable_reason"] = "Slot Exchange is not enabled for this equipment."
        return Response(payload)

    # The viewer's bookings the exchange could draw from: same equipment, or same group (whole bookings only).
    equipment_q = Q(equipment=equipment)
    if equipment.equipment_group_id:
        equipment_q |= Q(equipment__equipment_group_id=equipment.equipment_group_id)
    candidates = (
        Booking.objects.filter(equipment_q, user=user, status=BookingStatus.BOOKED)
        .filter(daily_slots__start_datetime__gt=now + sx.CUTOFF)
        .select_related("equipment")
        .distinct()
    )
    for mine in candidates:
        my_slots = list(mine.daily_slots.order_by("start_datetime"))
        if not my_slots or my_slots[0].start_datetime <= now:
            continue
        payload["my_bookings"].append(
            {
                "booking_id": mine.pk,
                "booking_ref": mine.virtual_booking_id or f"{mine.equipment.code}-#{mine.pk}",
                "equipment_name": mine.equipment.name,
                "same_equipment": mine.equipment_id == equipment.pk,
                "per_slot_charge": str(sx.per_slot_charge(mine, len(my_slots))),
                "slots": [sx.slot_summary(s) for s in my_slots],
            }
        )
    try:
        # A quick check of the simplest case (the clicked slot for one same-length slot) to say early why not.
        probe = next((b for b in payload["my_bookings"] if b["same_equipment"]), None)
        if probe:
            sx.plan_exchange(user, [probe["slots"][0]["id"]], [slot.pk], now=now)
        payload["exchange_available"] = bool(payload["my_bookings"])
        if not payload["my_bookings"]:
            payload["exchange_unavailable_reason"] = (
                "You have no upcoming booking on this equipment (or its group) to exchange."
            )
    except sx.ExchangeError as exc:
        # Count and length depend on the user's choice; anything else blocks every exchange with this booking.
        if exc.code in ("COUNT_MISMATCH", "LENGTH_MISMATCH", "PARTIAL_CROSS_EQUIPMENT"):
            payload["exchange_available"] = True
        else:
            payload["exchange_unavailable_reason"] = exc.message
    return Response(payload)


def _act(request, request_id, action):
    user = request.user
    try:
        if action == "accept":
            req = sx.accept_request(request_id, user)
        elif action == "decline":
            req = sx.decline_request(request_id, user, request.data.get("note") or "")
        else:
            req = sx.cancel_request(request_id, user)
    except sx.ExchangeError as exc:
        code = status.HTTP_404_NOT_FOUND if exc.code == "NOT_FOUND" else status.HTTP_409_CONFLICT
        return _error(exc, code)
    req = SlotExchangeRequest.objects.select_related(
        "requester", "responder", "requester_booking", "responder_booking", "responder_booking__equipment"
    ).get(pk=req.pk)
    return Response(sx.serialize_request(req, user))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def slot_exchange_accept(request, request_id):
    return _act(request, request_id, "accept")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def slot_exchange_decline(request, request_id):
    return _act(request, request_id, "decline")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def slot_exchange_cancel(request, request_id):
    return _act(request, request_id, "cancel")


__all__ = [
    "slot_exchanges",
    "slot_exchange_options",
    "slot_exchange_accept",
    "slot_exchange_decline",
    "slot_exchange_cancel",
    "SlotExchangeStatus",
]
