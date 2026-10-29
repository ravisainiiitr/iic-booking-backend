"""Site-wide search behind the header search bar: equipment, bookings and people, each limited to the viewer's role.

Equipment matching tolerates spelling mistakes and understands techniques (``search_index``); bookings and people
match exact or contained text only, so a near-miss never reveals someone else's record.
"""

from __future__ import annotations

import re
from typing import Any

from django.db.models import Case, IntegerField, Q, Value, When
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

MIN_CHARS = 2
MAX_CHARS = 80
EQUIPMENT_LIMIT = 6
BOOKING_LIMIT = 8
PEOPLE_LIMIT = 6
_DISPLAY_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*-(\d+)$")
# Roles whose people search covers every user (others: only users within their bookings scope).
LOOKS_UP_ANY_USER = frozenset({"admin", "manager", "operator", "dept_admin"})


def _searchable_equipment_ids(user) -> set[int]:
    """Equipment the viewer may see in the catalogue, without test equipment (kept for test accounts)."""
    from .api_views import get_visible_equipment_queryset
    from .testdata import exclude_test_equipment, is_test_user

    scope = "all" if getattr(user, "is_authenticated", False) else None
    qs = get_visible_equipment_queryset(user, catalog_scope=scope, mode_catalog_filter=False)
    if not is_test_user(user):
        qs = exclude_test_equipment(qs)
    return set(qs.values_list("equipment_id", flat=True))


def _search_equipment(user, q: str) -> tuple[list[dict[str, Any]], str]:
    """Ranked equipment hits (each with ``match`` kind and hint) and a "did you mean" query or ``""``."""
    from .models import Equipment
    from .search_index import rank

    ranked, suggestion = rank(q, _searchable_equipment_ids(user), EQUIPMENT_LIMIT)
    rows = Equipment.objects.select_related("category", "internal_department").in_bulk([r["id"] for r in ranked])
    out = []
    for r in ranked:
        e = rows.get(r["id"])
        if e is None:
            continue
        out.append(
            {
                "id": e.equipment_id,
                "name": e.name,
                "code": e.code,
                "category": e.category.name if e.category_id else "",
                "department": e.internal_department.name if e.internal_department_id else "",
                "status": e.status or "",
                "match": r["kind"],
                "hint": r["hint"],
            }
        )
    from .mode_utils import attach_mode_inactive

    attach_mode_inactive(out, user, id_key="id")
    return out, suggestion


def _status_codes_matching(q: str) -> list[str]:
    from .models import BookingStatus

    needle = q.lower()
    return [code for code, label in BookingStatus.choices if len(needle) >= 4 and str(label).lower().startswith(needle)]


def _search_bookings(user, q: str, *, staff_view: bool) -> list[dict[str, Any]]:
    from .admin_insights.user_card import _slot_annotations
    from .api_views import bookings_visible_to

    from .api_views import faculty_wallet_student_ids

    match = Q(virtual_booking_id__icontains=q) | Q(equipment__name__icontains=q) | Q(equipment__code__iexact=q)
    if staff_view:
        match |= Q(user__name__icontains=q) | Q(user__email__icontains=q) | Q(user__phone_number__icontains=q)
        # A supervisor's name also finds the bookings of their approved wallet students.
        students = faculty_wallet_student_ids(
            Q(name__icontains=q) | Q(email__icontains=q) | Q(phone_number__icontains=q)
        )
        if students:
            match |= Q(user_id__in=students)
    if q.isdigit():
        match |= Q(booking_id=int(q))
    display = _DISPLAY_ID.match(q)
    if display:
        match |= Q(booking_id=int(display.group(1)))
    statuses = _status_codes_matching(q)
    if statuses:
        match |= Q(status__in=statuses)

    qs = (
        bookings_visible_to(user)
        .filter(match)
        .select_related("equipment", "user")
        .annotate(**_slot_annotations())
        .order_by("-created_at", "-pk")
    )
    rows = []
    for b in qs[:BOOKING_LIMIT]:
        rows.append(
            {
                "id": b.booking_id,
                "virtual_booking_id": b.virtual_booking_id or f"{b.equipment.code}-{b.booking_id}",
                "status": b.status,
                "status_label": b.get_status_display(),
                "equipment": b.equipment.name,
                "equipment_code": b.equipment.code,
                "user_name": (b.user.name or b.user.email) if b.user_id and b.user_id != user.pk else "",
                "slot_start": b.slot_start.isoformat() if b.slot_start else None,
                "created_at": b.created_at.isoformat() if b.created_at else None,
                "own": b.user_id == user.pk,
            }
        )
    return rows


def _people_scope(user):
    """Users the viewer may look up, or None when people search is not offered to this role."""
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    from .api_views import check_operator_permission

    if getattr(user, "is_superuser", False) or user.user_type in LOOKS_UP_ANY_USER:
        # Main Admin, and lab staff (OIC, Lab Operator, Department Administrator), find any user.
        return User.objects.all()
    if not (check_operator_permission(user) or user.user_type in (UserType.DEPT_ADMIN, UserType.FINANCE)):
        return None
    from .admin_insights.user_card import _scoped_bookings

    return User.objects.filter(pk__in=_scoped_bookings(user).values("user_id"))


def _search_people(user, q: str) -> list[dict[str, Any]] | None:
    from iic_booking.users.models.user_type import UserType

    scope = _people_scope(user)
    if scope is None:
        return None
    qs = (
        scope.filter(is_active=True)
        .filter(Q(name__icontains=q) | Q(email__icontains=q) | Q(phone_number__icontains=q))
        .select_related("department")
        .annotate(
            rank=Case(
                When(name__istartswith=q, then=Value(0)),
                When(email__istartswith=q, then=Value(1)),
                default=Value(2),
                output_field=IntegerField(),
            )
        )
        .order_by("rank", "name")
    )
    labels = {code: str(label) for code, label in UserType.get_choices()}
    return [
        {
            "id": u.pk,
            "name": u.name or u.email,
            "email": u.email,
            "user_type": labels.get(u.user_type, u.user_type or ""),
            "department": u.department.name if u.department_id else "",
        }
        for u in qs[:PEOPLE_LIMIT]
    ]


LAB_STAFF_LIMIT = 6


def _search_lab_staff(user, q: str) -> list[dict[str, Any]]:
    """Officers in Charge and Lab In-charges (operators) whose name matches, with the instruments they look after
    that the viewer may see. Only the contact details the equipment page already shows (name, email)."""
    from iic_booking.users.display import name_with_honorific

    from .models import EquipmentManager, EquipmentOperator

    visible = _searchable_equipment_ids(user)
    if not visible:
        return []
    people: dict[tuple[int, str], dict[str, Any]] = {}
    for model, field, role in (
        (EquipmentManager, "manager", "Officer in Charge"),
        (EquipmentOperator, "operator", "Lab In-charge"),
    ):
        links = (
            model.objects.filter(equipment_id__in=visible, **{f"{field}__is_active": True})
            .filter(**{f"{field}__name__icontains": q})
            .select_related(field, "equipment")
            .order_by(f"{field}__name", "equipment__name")
        )
        for link in links[:60]:
            person = getattr(link, field)
            entry = people.setdefault((person.pk, role), {
                "id": person.pk,
                "name": name_with_honorific(person, getattr(link, "honorific", ""), default=person.get_display_name()),
                "role": role,
                "email": person.email,
                "equipment": [],
            })
            if all(e["id"] != link.equipment_id for e in entry["equipment"]):
                entry["equipment"].append({"id": link.equipment_id, "name": link.equipment.name,
                                           "code": link.equipment.code})
    rows = sorted(people.values(), key=lambda p: (not p["name"].lower().startswith(q.lower()), p["name"].lower()))
    return rows[:LAB_STAFF_LIMIT]


@api_view(["GET"])
@permission_classes([AllowAny])
def global_search(request):
    """GET ?q= : grouped matches. Anonymous visitors get public equipment only."""
    q = " ".join((request.query_params.get("q") or "").split())[:MAX_CHARS]
    result: dict[str, Any] = {
        "query": q,
        "equipment": [],
        "bookings": [],
        "people": [],
        "people_enabled": False,
        "lab_staff": [],
        "suggestion": "",
    }
    if len(q) < MIN_CHARS:
        return Response(result)
    user = request.user
    result["equipment"], result["suggestion"] = _search_equipment(user, q)
    if getattr(user, "is_authenticated", False):
        from .api_views import check_operator_permission
        from iic_booking.users.models.user_type import UserType

        staff_view = check_operator_permission(user) or user.user_type in (UserType.DEPT_ADMIN, UserType.FINANCE)
        result["bookings"] = _search_bookings(user, q, staff_view=staff_view)
        people = _search_people(user, q)
        result["people_enabled"] = people is not None
        result["people"] = people or []
        result["staff_view"] = staff_view
        # Users find the Officer in Charge and Lab In-charge of the instruments they can see, by name.
        result["lab_staff"] = _search_lab_staff(user, q)
    return Response(result)
