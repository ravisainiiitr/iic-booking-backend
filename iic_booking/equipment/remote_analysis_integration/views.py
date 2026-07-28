"""HTTP views — booking analysis integration APIs."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.equipment.models import Booking
from iic_booking.equipment.remote_analysis_integration.dashboard import BookingAnalysisDashboardService
from iic_booking.equipment.remote_analysis_integration.reports import BookingReportBridge
from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService


def _can_access_booking(user, booking: Booking) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type in {"admin", "dept_admin", "manager", "officer_in_charge", "operator"}:
        return True
    if booking.user_id == user.pk:
        return True
    if booking.created_by_id == user.pk:
        return True
    # Faculty supervision: same department
    if getattr(user, "department_id", None) and getattr(booking.user, "department_id", None):
        if user.department_id == booking.user.department_id and user_type in {"faculty", "staff"}:
            return True
    return False


def _can_launch(user, booking: Booking) -> bool:
    if getattr(user, "is_superuser", False):
        return True
    return booking.user_id == user.pk


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_detail(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/"""
    booking = get_object_or_404(
        Booking.objects.select_related("equipment", "user", "analysis_reservation", "analysis_workspace"),
        booking_id=booking_id,
    )
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    payload = BookingRemoteAnalysisService().get_summary(booking)
    payload["report"] = BookingReportBridge().enrich_booking(booking)
    return Response(payload)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_create(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/create/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        reservation = BookingRemoteAnalysisService().ensure_reservation(booking, actor=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(
        {"reservation_id": str(reservation.id), "status": reservation.status},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_launch(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/launch/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_launch(request.user, booking):
        return Response({"detail": "Only the booking owner may launch the desktop."}, status=status.HTTP_403_FORBIDDEN)
    try:
        session = BookingRemoteAnalysisService().launch_session(
            booking,
            user=request.user,
            client_ip=request.META.get("REMOTE_ADDR"),
        )
    except Exception as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"session_id": str(session.id), "status": session.status}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_files(request, booking_id: int):
    """GET /api/v1/bookings/{id}/analysis/files/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    files = BookingRemoteAnalysisService().workspace.list_files(booking)
    return Response(files)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_analysis_archive(request, booking_id: int):
    """POST /api/v1/bookings/{id}/analysis/archive/"""
    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), booking_id=booking_id)
    if not _can_access_booking(request.user, booking):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    try:
        archive = BookingRemoteAnalysisService().archive_workspace(booking, actor=request.user)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"archive_id": str(getattr(archive, "id", "")), "ok": True})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_analysis_dashboard(request):
    """GET /api/v1/bookings/analysis/dashboard/?scope=user|faculty|lab"""
    scope = (request.query_params.get("scope") or "user").lower()
    svc = BookingAnalysisDashboardService()
    if scope == "lab":
        return Response(svc.for_lab(request.user))
    if scope == "faculty":
        return Response(svc.for_faculty(request.user))
    return Response(svc.for_user(request.user))
