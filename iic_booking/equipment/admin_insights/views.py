"""GET endpoints for the dashboard insight pages (same access as the admin dashboard summary)."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.admin_dashboard_summary import can_view_admin_dashboard

FORBIDDEN = "Only the Main Administrator or a Department Administrator can view this overview."


def _respond(request, build):
    if not can_view_admin_dashboard(request.user):
        return Response({"error": FORBIDDEN}, status=status.HTTP_403_FORBIDDEN)
    return Response(build(request.user, request.query_params), status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_insights_equipment(request):
    """GET /api/admin/insights/equipment/ — equipment behind the dashboard's Equipment card."""
    from .equipment import build_equipment_insights

    return _respond(request, build_equipment_insights)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_insights_users(request):
    """GET /api/admin/insights/users/ — users behind the dashboard's Active Users card."""
    from .users import build_user_insights

    return _respond(request, build_user_insights)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_insights_cancellations(request):
    """GET /api/admin/insights/cancellations/ — who cancelled, why, how late and what was refunded."""
    from .cancellations import build_cancellation_insights

    return _respond(request, build_cancellation_insights)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_insights_refund_requests(request):
    """GET /api/admin/insights/refund-requests/ — users cancelling their own bookings for a refund before the slot."""
    from .refund_requests import build_refund_request_insights

    return _respond(request, build_refund_request_insights)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_insights_staff_proficiency(request):
    """GET /api/admin/insights/staff-proficiency/ — Lab Operators and OICs ranked by how short they keep their queue."""
    from .proficiency import build_staff_proficiency

    return _respond(request, build_staff_proficiency)


def _card_response(request, build):
    from .user_card import CardError

    if not can_view_admin_dashboard(request.user):
        return Response({"error": FORBIDDEN}, status=status.HTTP_403_FORBIDDEN)
    try:
        return Response(build(), status=status.HTTP_200_OK)
    except CardError as exc:
        return Response({"error": exc.message}, status=exc.status)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_insights_user_card(request, user_id: int):
    """GET /api/admin/insights/users/<id>/ — the user's ID card, booking totals and recent bookings."""
    from .user_card import build_user_card

    return _card_response(request, lambda: build_user_card(request.user, user_id, request))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_insights_wallet_bookings(request, user_id: int):
    """GET /api/admin/insights/users/<id>/wallet-bookings/ — bookings by every user linked to the user's wallet."""
    from .user_card import build_wallet_bookings

    return _card_response(request, lambda: build_wallet_bookings(request.user, user_id, request.query_params))
