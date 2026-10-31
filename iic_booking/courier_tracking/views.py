"""Sample arrival API (mounted at /api/v1/courier-tracking/).

Users: courier list, their booking's arrival (eligibility + declaration: hand delivery, or a courier name and tracking
number typed in), their shipments. Staff: shipments of equipment in their existing scope (Admin all; OIC / Department
Admin / Lab Operator by equipment; Accounts In Charge: external bookings). Automatic tracking with a courier-tracking
provider was removed (31 Oct 2026 committee: no recurring cost); nothing here calls an outside service. Department
enablement is managed in Department Modules (/api/v1/admin/department-modules/).
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_date
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.department_modules import access as module_access
from iic_booking.users.models.user_type import UserType

from . import services
from .constants import ArrivalMode
from .eligibility import arrival_eligibility
from .models import SampleArrival
from .serializers import ArrivalRequestSerializer, arrival_dict

logger = logging.getLogger(__name__)

STAFF_TYPES = (UserType.ADMIN, UserType.MANAGER, UserType.DEPT_ADMIN, UserType.OPERATOR, UserType.FINANCE)


# -- scope -----------------------------------------------------------------------------------------------------------

def _staff_equipment_ids(user):
    """None: everything; list: these equipment; "external": external bookings (Accounts In Charge)."""
    if module_access.is_main_admin(user):
        return None
    if user.user_type == UserType.FINANCE:
        return "external"
    if user.user_type in (UserType.MANAGER, UserType.DEPT_ADMIN, UserType.OPERATOR):
        from iic_booking.equipment.api_views import _get_equipment_ids_for_log_access

        return list(_get_equipment_ids_for_log_access(user) or [])
    return []


def _is_staff(user) -> bool:
    return module_access.is_main_admin(user) or getattr(user, "user_type", None) in STAFF_TYPES


def _staff_queryset(user):
    scope = _staff_equipment_ids(user)
    qs = SampleArrival.objects.all()
    if scope is None:
        return qs
    if scope == "external":
        return qs.filter(booking__user_type_snapshot__in=UserType.get_external_user_codes())
    return qs.filter(equipment_id__in=scope)


def _can_view(user, arrival: SampleArrival) -> bool:
    if arrival.booking.user_id == user.id:
        return True
    return _is_staff(user) and _staff_queryset(user).filter(pk=arrival.pk).exists()


def _booking_for(user, booking_id):
    from iic_booking.equipment.models import Booking

    booking = get_object_or_404(Booking.objects.select_related("equipment", "user"), pk=booking_id)
    if booking.user_id == user.id:
        return booking, False
    if _is_staff(user):
        scope = _staff_equipment_ids(user)
        visible = (
            scope is None
            or (scope == "external" and booking.user_type_snapshot in UserType.get_external_user_codes())
            or (isinstance(scope, list) and booking.equipment_id in scope)
        )
        if visible:
            return booking, True
    return None, False


def _base_qs():
    return SampleArrival.objects.select_related("booking__user", "booking__equipment", "equipment", "department")


# -- couriers --------------------------------------------------------------------------------------------------------

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def couriers(request):
    """Couriers to choose from (the built-in list)."""
    items = services.catalog()
    if not items:
        return Response({"error": "The courier list is not available right now. Please try again.",
                         "code": "catalog_unavailable"}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    response = Response({"couriers": items, "count": len(items)})
    response["Cache-Control"] = "private, max-age=3600"
    return response


# -- booking arrival -------------------------------------------------------------------------------------------------

@transaction.non_atomic_requests  # the booking row lock in register_arrival is the transaction that matters
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_arrival(request, booking_id: int):
    booking, staff_view = _booking_for(request.user, booking_id)
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "GET":
        elig = arrival_eligibility(request.user, booking)
        arrivals = list(_base_qs().filter(booking=booking).order_by("-created_at")[:20])
        active = next((a for a in arrivals if a.is_active), None)
        return Response(
            {
                "booking_id": booking.pk,
                "eligibility": elig.as_dict(),
                "active": arrival_dict(active, staff=staff_view) if active else None,
                "history": [arrival_dict(a, staff=staff_view, include_history=False) for a in arrivals if not a.is_active],
            }
        )
    if staff_view:
        return Response({"error": "Only the person who made the booking can declare how the sample arrives."},
                        status=status.HTTP_403_FORBIDDEN)
    serializer = ArrivalRequestSerializer(data=request.data)
    if not serializer.is_valid():
        return Response({"error": "Please check the highlighted fields.", "fields": serializer.errors},
                        status=status.HTTP_400_BAD_REQUEST)
    data = serializer.validated_data
    try:
        arrival = services.register_arrival(
            request.user,
            booking,
            services.ArrivalInput(
                arrival_mode=data["arrival_mode"],
                courier_slug=data.get("courier_slug", ""),
                tracking_number=data.get("tracking_number", ""),
                sample_identifiers=data.get("sample_identifiers", ""),
                dispatch_date=data.get("dispatch_date"),
                expected_arrival_date=data.get("expected_arrival_date"),
                notes=data.get("notes", ""),
            ),
        )
    except services.ArrivalError as exc:
        body = {"error": exc.message, "code": exc.code}
        if exc.field:
            body["fields"] = {exc.field: [exc.message]}
        return Response(body, status=exc.status)
    arrival = _base_qs().get(pk=arrival.pk)
    message = (
        "Courier details saved. The lab can see the courier and tracking number, and will confirm when the sample "
        "arrives."
        if arrival.arrival_mode == ArrivalMode.COURIER
        else "Manual delivery recorded. Bring the sample to the lab; staff will confirm when they receive it."
    )
    return Response({"message": message, "arrival": arrival_dict(arrival)}, status=status.HTTP_201_CREATED)


# -- shipments -------------------------------------------------------------------------------------------------------

def _apply_filters(qs, params):
    def _int(name):
        try:
            return int(params.get(name)) if params.get(name) not in (None, "") else None
        except (TypeError, ValueError):
            return None

    for field, name in (("department_id", "department"), ("equipment_id", "equipment"), ("booking_id", "booking"),
                        ("booking__user_id", "user")):
        value = _int(name)
        if value is not None:
            qs = qs.filter(**{field: value})
    if params.get("courier"):
        qs = qs.filter(courier_slug=str(params["courier"]).strip().lower())
    if params.get("status"):
        qs = qs.filter(status__in=[s.strip().upper() for s in str(params["status"]).split(",") if s.strip()])
    if params.get("arrival_mode"):
        qs = qs.filter(arrival_mode=str(params["arrival_mode"]).upper())
    if params.get("sample"):
        qs = qs.filter(sample_identifiers__icontains=str(params["sample"])[:100])
    text = str(params.get("q") or "").strip()[:100]
    if text:
        qs = qs.filter(
            Q(equipment__code__icontains=text) | Q(equipment__name__icontains=text)
            | Q(department__name__icontains=text) | Q(booking__virtual_booking_id__icontains=text)
            | Q(booking__user__email__icontains=text) | Q(booking__user__name__icontains=text)
            | Q(tracking_number__icontains=text.replace(" ", "").upper()) | Q(sample_identifiers__icontains=text)
            | Q(courier_name__icontains=text)
        )
    for name, lookup in (("expected_from", "expected_arrival_date__gte"), ("expected_to", "expected_arrival_date__lte")):
        day = parse_date(str(params.get(name) or ""))
        if day:
            qs = qs.filter(**{lookup: day})
    if str(params.get("overdue") or "").lower() in ("1", "true", "yes"):
        qs = qs.filter(expected_arrival_date__lt=timezone.localdate(), is_active=True)
    if str(params.get("include_inactive") or "").lower() not in ("1", "true", "yes"):
        qs = qs.filter(is_active=True)
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def shipments(request):
    user = request.user
    staff = _is_staff(user) and str(request.query_params.get("view") or "") == "staff"
    qs = _staff_queryset(user) if staff else SampleArrival.objects.filter(booking__user=user)
    qs = _apply_filters(qs.select_related("booking__user", "booking__equipment", "equipment", "department"),
                        request.query_params).order_by("-created_at")
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    size = 25
    total = qs.count()
    rows = list(qs[(page - 1) * size: page * size])
    return Response(
        {
            "count": total,
            "page": page,
            "page_size": size,
            "results": [arrival_dict(a, staff=staff, include_history=False) for a in rows],
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def shipment_detail(request, pk: int):
    arrival = get_object_or_404(_base_qs(), pk=pk)
    if not _can_view(request.user, arrival):
        return Response({"error": "Shipment not found."}, status=status.HTTP_404_NOT_FOUND)
    staff = arrival.booking.user_id != request.user.id
    return Response(arrival_dict(arrival, staff=staff))
