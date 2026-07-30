"""Reservation / scheduler API views (Milestone 3)."""

from __future__ import annotations

from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.remote_analysis.permissions import CanManageRemoteAnalysis, CanViewRemoteAnalysis
from iic_booking.remote_analysis.scheduler_models import (
    AnalysisReservation,
    SoftwareRequirement,
)
from iic_booking.remote_analysis.selectors import reservations as reservation_selectors
from iic_booking.remote_analysis.serializers import (
    AnalysisReservationSerializer,
    CreateReservationSerializer,
    ExtendReservationSerializer,
    MaintenanceWindowSerializer,
    ReservationQueueSerializer,
)
from iic_booking.remote_analysis.services.allocation import AllocationService
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.scheduler import SchedulerService

_MANAGE = [IsAuthenticated, CanManageRemoteAnalysis]
_VIEW = [IsAuthenticated, CanViewRemoteAnalysis]


def _department_scope(request):
    user = request.user
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type == "admin" or getattr(user, "is_superuser", False):
        return None
    return getattr(user, "department_id", None)


@api_view(["GET", "POST"])
@permission_classes(_VIEW)
def reservations_collection(request):
    """GET/POST /api/v1/analysis/reservations/"""
    if request.method == "GET":
        qs = reservation_selectors.reservations_queryset(department_id=_department_scope(request))
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter.upper())
        from iic_booking.remote_analysis.production_hardening import parse_pagination

        offset, limit = parse_pagination(request)
        return Response(AnalysisReservationSerializer(qs[offset : offset + limit], many=True).data)

    # POST — managers only
    if not CanManageRemoteAnalysis().has_permission(request, None):
        return Response({"detail": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

    ser = CreateReservationSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data

    booking = None
    user = request.user
    department = getattr(request.user, "department", None)

    if data.get("booking_id"):
        from iic_booking.equipment.models import Booking

        booking = get_object_or_404(Booking, booking_id=data["booking_id"])
        user = booking.user
        department = getattr(booking.user, "department", None) or getattr(
            booking.equipment, "internal_department", None
        )
    elif data.get("user_id"):
        from django.contrib.auth import get_user_model

        user = get_object_or_404(get_user_model(), pk=data["user_id"])
        department = getattr(user, "department", None)

    if data.get("department_id"):
        from iic_booking.users.models import Department

        department = get_object_or_404(Department, pk=data["department_id"])

    software_profile = None
    if data.get("software_profile_id"):
        software_profile = get_object_or_404(SoftwareRequirement, pk=data["software_profile_id"])

    try:
        if booking is None:
            if not data.get("requested_start") or not data.get("requested_end"):
                return Response(
                    {"detail": "requested_start and requested_end are required without booking_id"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            start = ReservationService.parse_dt(data["requested_start"])
            end = ReservationService.parse_dt(data["requested_end"])
        else:
            start = ReservationService.parse_dt(data["requested_start"]) if data.get("requested_start") else timezone.now()
            end = ReservationService.parse_dt(data["requested_end"]) if data.get("requested_end") else start

        reservation = ReservationService().create_reservation(
            user=user,
            requested_start=start,
            requested_end=end,
            booking=booking,
            department=department,
            software_profile=software_profile,
            requested_capabilities=data.get("requested_capabilities") or {},
            requested_resources=data.get("requested_resources") or {},
            priority=data.get("priority", 100),
            created_by=request.user,
            auto_allocate=data.get("auto_allocate", True),
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

    return Response(AnalysisReservationSerializer(reservation).data, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes(_VIEW)
def reservation_detail(request, reservation_id):
    reservation = get_object_or_404(AnalysisReservation, pk=reservation_id)
    data = AnalysisReservationSerializer(reservation).data
    data["history"] = [
        {
            "from_status": h.from_status,
            "to_status": h.to_status,
            "reason": h.reason,
            "created_at": h.created_at.isoformat(),
        }
        for h in reservation.history.all()[:50]
    ]
    return Response(data)


@api_view(["POST"])
@permission_classes(_MANAGE)
def reservation_cancel(request, reservation_id):
    reservation = get_object_or_404(AnalysisReservation, pk=reservation_id)
    reason = str(request.data.get("reason") or "Cancelled by administrator")
    try:
        ReservationService().cancel(reservation, actor=request.user, reason=reason)
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(AnalysisReservationSerializer(reservation).data)


@api_view(["POST"])
@permission_classes(_MANAGE)
def reservation_extend(request, reservation_id):
    reservation = get_object_or_404(AnalysisReservation, pk=reservation_id)
    ser = ExtendReservationSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    try:
        ReservationService().extend(
            reservation,
            ReservationService.parse_dt(ser.validated_data["new_end"]),
            actor=request.user,
        )
    except ValueError as exc:
        return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(AnalysisReservationSerializer(reservation).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def availability(request):
    start = parse_datetime(request.query_params.get("start") or "") or timezone.now()
    end = parse_datetime(request.query_params.get("end") or "") or (start + timezone.timedelta(hours=2))
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    if timezone.is_naive(end):
        end = timezone.make_aware(end)

    engine = AvailabilityEngine()
    available = engine.list_available(start, end, department_id=_department_scope(request))
    return Response(
        {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "count": len(available),
            "workstations": [
                {
                    "id": str(ws.id),
                    "hostname": ws.hostname,
                    "display_name": ws.display_name,
                    "status": ws.status,
                    "health_score": ws.health_score,
                    "availability": result.to_dict(),
                }
                for ws, result in available
            ],
        }
    )


@api_view(["GET"])
@permission_classes(_VIEW)
def candidates(request):
    start = parse_datetime(request.query_params.get("start") or "") or timezone.now()
    end = parse_datetime(request.query_params.get("end") or "") or (start + timezone.timedelta(hours=2))
    if timezone.is_naive(start):
        start = timezone.make_aware(start)
    if timezone.is_naive(end):
        end = timezone.make_aware(end)

    ranked = AllocationService().rank_candidates(
        start=start,
        end=end,
        department_id=_department_scope(request),
        user=request.user,
        include_unavailable=request.query_params.get("include_unavailable") == "1",
    )
    return Response({"count": len(ranked), "candidates": [c.to_dict() for c in ranked[:50]]})


@api_view(["GET"])
@permission_classes(_VIEW)
def scheduler_status(request):
    return Response(SchedulerService().status())


@api_view(["GET"])
@permission_classes(_VIEW)
def reservation_queue(request):
    entries = reservation_selectors.queue_entries(limit=100)
    return Response(ReservationQueueSerializer(entries, many=True).data)


@api_view(["GET"])
@permission_classes(_VIEW)
def scheduler_dashboard(request):
    dept = _department_scope(request)
    now = timezone.now()
    return Response(
        {
            "scheduler": SchedulerService().status(),
            "statistics": reservation_selectors.allocation_statistics(department_id=dept),
            "upcoming": AnalysisReservationSerializer(
                reservation_selectors.upcoming_reservations(department_id=dept), many=True
            ).data,
            "expired": AnalysisReservationSerializer(
                reservation_selectors.expired_reservations(department_id=dept), many=True
            ).data,
            "queue": ReservationQueueSerializer(reservation_selectors.queue_entries(), many=True).data,
            "maintenance": MaintenanceWindowSerializer(
                reservation_selectors.maintenance_windows(), many=True
            ).data,
            "calendar": AnalysisReservationSerializer(
                reservation_selectors.calendar_events(
                    start=now - timezone.timedelta(days=1),
                    end=now + timezone.timedelta(days=14),
                    department_id=dept,
                ),
                many=True,
            ).data,
            "available_now": AvailabilityEngine()
            .list_available(now, now + timezone.timedelta(hours=1), department_id=dept).__len__(),
        }
    )
