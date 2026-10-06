"""OIC Substitute API (OIC dashboard menu "OIC Substitute"; Main Administrator view of all substitutions)."""

from __future__ import annotations

from django.db.models import Q
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import EquipmentTemporaryOIC
from .oic_substitution import (
    MAX_PERIOD_DAYS,
    MAX_SUBSTITUTES,
    SubstitutionError,
    create_substitution,
    delegation_queryset,
    delegation_to_dict,
    end_substitution,
    is_main_admin,
    is_oic_user,
    permanent_oic_equipment_queryset,
    search_candidates,
)

ADMIN_LIST_LIMIT = 300
OIC_LIST_LIMIT = 200

_FORBIDDEN = {"error": "Only an Officer in Charge (OIC) or the Main Administrator can open OIC Substitute."}


def _error(exc: SubstitutionError) -> Response:
    return Response({"error": exc.message}, status=exc.status_code)


def _department(user):
    dept = getattr(user, "department", None)
    return {"id": dept.pk, "name": dept.name} if dept else None


def _filter_by_status(qs, value: str, now):
    value = (value or "").strip().lower()
    if value == "active":
        return qs.active(now)
    if value == "scheduled":
        return qs.open(now).filter(start_at__gt=now)
    if value == "past":
        return qs.exclude(pk__in=EquipmentTemporaryOIC.objects.open(now).values("pk"))
    return qs


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_substitute_options(request):
    user = request.user
    if is_main_admin(user):
        return Response({"role": "admin", "equipments": [], "department": None})
    if not is_oic_user(user):
        return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    return Response(
        {
            "role": "oic",
            "department": _department(user),
            "equipments": [
                {"id": e.pk, "code": e.code or "", "name": e.name or ""} for e in permanent_oic_equipment_queryset(user)
            ],
            "max_substitutes": MAX_SUBSTITUTES,
            "max_period_days": MAX_PERIOD_DAYS,
            "today": timezone.localdate().isoformat(),
        }
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_substitute_candidates(request):
    """Active OICs of the requesting OIC's department (never other departments)."""
    user = request.user
    if not is_oic_user(user):
        return Response(
            {"error": "Only an Officer in Charge (OIC) can search for substitutes."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from iic_booking.users.display import get_user_display_name

    return Response(
        {
            "department": _department(user),
            "candidates": [
                {"id": u.pk, "name": get_user_display_name(u) or "", "email": u.email or ""}
                for u in search_candidates(user, request.GET.get("search", ""))
            ],
        }
    )


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def oic_substitutes(request):
    user = request.user
    now = timezone.now()
    if request.method == "POST":
        try:
            rows = create_substitution(
                primary=user,
                equipment_id=request.data.get("equipment_id"),
                substitute_ids=request.data.get("substitute_ids") or [],
                start_date=request.data.get("start_date"),
                end_date=request.data.get("end_date"),
                reason=request.data.get("reason"),
                now=now,
            )
        except SubstitutionError as exc:
            return _error(exc)
        items = [delegation_to_dict(d, viewer=user, now=now) for d in delegation_queryset().filter(pk__in=[r.pk for r in rows])]
        names = ", ".join(i["substitute"]["name"] or i["substitute"]["email"] for i in items)
        return Response(
            {
                "items": items,
                "message": f"{names} can manage this equipment as OIC substitute for the chosen period. "
                "They and the Lab in-charges have been notified.",
            },
            status=status.HTTP_201_CREATED,
        )

    if is_main_admin(user):
        qs = _filter_by_status(delegation_queryset(), request.GET.get("status", ""), now)
        search = (request.GET.get("search") or "").strip()
        if search:
            qs = qs.filter(
                Q(equipment__code__icontains=search)
                | Q(equipment__name__icontains=search)
                | Q(primary_oic__name__icontains=search)
                | Q(primary_oic__email__icontains=search)
                | Q(temporary_oic__name__icontains=search)
                | Q(temporary_oic__email__icontains=search)
            )
        rows = list(qs.order_by("-created_at", "-id")[:ADMIN_LIST_LIMIT])
        return Response(
            {"scope": "admin", "items": [delegation_to_dict(d, viewer=user, now=now) for d in rows], "limit": ADMIN_LIST_LIMIT}
        )
    if not is_oic_user(user):
        return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
    base = delegation_queryset().order_by("-created_at", "-id")
    granted = list(base.filter(primary_oic=user)[:OIC_LIST_LIMIT])
    assigned = list(base.filter(temporary_oic=user)[:OIC_LIST_LIMIT])
    return Response(
        {
            "scope": "oic",
            "granted": [delegation_to_dict(d, viewer=user, now=now) for d in granted],
            "assigned_to_me": [delegation_to_dict(d, viewer=user, now=now) for d in assigned],
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_substitute_end(request, delegation_id: int):
    """Cancel a scheduled substitution or revoke an active one; a reason is required."""
    user = request.user
    qs = delegation_queryset()
    if not is_main_admin(user):
        if not is_oic_user(user):
            return Response(_FORBIDDEN, status=status.HTTP_403_FORBIDDEN)
        qs = qs.filter(primary_oic=user)
    delegation = qs.filter(pk=delegation_id).first()
    if delegation is None:
        return Response({"error": "Substitution not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        delegation = end_substitution(delegation=delegation, actor=user, reason=request.data.get("reason"))
    except SubstitutionError as exc:
        return _error(exc)
    item = delegation_to_dict(delegation_queryset().get(pk=delegation.pk), viewer=user)
    verb = "cancelled" if item["status"] == EquipmentTemporaryOIC.Status.CANCELLED else "revoked"
    name = item["substitute"]["name"] or item["substitute"]["email"]
    return Response(
        {
            "item": item,
            "message": f"Substitution {verb}. {name} no longer has OIC access to this equipment; "
            "they and the Lab in-charges have been notified.",
        }
    )
