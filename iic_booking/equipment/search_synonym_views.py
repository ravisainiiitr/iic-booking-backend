"""Main Administrator: search synonyms (techniques and the words people type for them) used by the header search."""

from __future__ import annotations

from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .models import Equipment, EquipmentCategory
from .search_synonym_models import SearchSynonym, split_terms
from .testdata import exclude_test_categories, exclude_test_equipment

MAX_TERMS = 60


def _is_main_admin(user) -> bool:
    return bool(user and (getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN))


def _row(syn: SearchSynonym) -> dict:
    return {
        "id": syn.pk,
        "technique": syn.technique,
        "terms": syn.term_list(),
        "category_ids": sorted(c.pk for c in syn.categories.all()),
        "equipment_ids": sorted(e.pk for e in syn.equipment.all()),
        "is_active": syn.is_active,
        "updated_at": syn.updated_at.isoformat() if syn.updated_at else None,
    }


def _options() -> dict:
    categories = exclude_test_categories(EquipmentCategory.objects.order_by("name"))
    equipment = exclude_test_equipment(Equipment.objects.order_by("name"))
    return {
        "categories": [{"id": c.pk, "name": c.name} for c in categories],
        "equipment": [{"id": e.equipment_id, "code": e.code, "name": e.name} for e in equipment],
    }


def _ids(raw, model) -> list[int] | None:
    if raw is None:
        return None
    try:
        wanted = {int(x) for x in raw}
    except (TypeError, ValueError):
        raise ValueError("Ids must be numbers") from None
    found = set(model.objects.filter(pk__in=wanted).values_list("pk", flat=True))
    if wanted - found:
        raise ValueError("Some selected categories or equipment no longer exist")
    return sorted(found)


def _apply(syn: SearchSynonym, data, user, *, creating: bool) -> SearchSynonym:
    if creating or "technique" in data:
        technique = " ".join(str(data.get("technique") or "").split())[:120]
        if not technique:
            raise ValueError("Enter the technique name")
        syn.technique = technique
    if creating or "terms" in data:
        terms = split_terms(data.get("terms") or [])
        if not terms:
            raise ValueError("Enter at least one search term")
        if len(terms) > MAX_TERMS:
            raise ValueError(f"At most {MAX_TERMS} search terms per technique")
        syn.terms = ", ".join(terms)
    if "is_active" in data:
        syn.is_active = bool(data.get("is_active"))
    category_ids = _ids(data.get("category_ids"), EquipmentCategory)
    equipment_ids = _ids(data.get("equipment_ids"), Equipment)
    syn.updated_by = user
    syn.save()
    if category_ids is not None:
        syn.categories.set(category_ids)
    if equipment_ids is not None:
        syn.equipment.set(equipment_ids)
    return syn


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def search_synonym_list(request):
    """GET: every synonym with category / equipment choices. POST: add one."""
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator access required"}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        rows = SearchSynonym.objects.prefetch_related("categories", "equipment")
        return Response({"synonyms": [_row(s) for s in rows], **_options()})
    try:
        syn = _apply(SearchSynonym(), request.data or {}, request.user, creating=True)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_row(syn), status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def search_synonym_detail(request, pk: int):
    if not _is_main_admin(request.user):
        return Response({"error": "Main Administrator access required"}, status=status.HTTP_403_FORBIDDEN)
    syn = SearchSynonym.objects.filter(pk=pk).first()
    if syn is None:
        return Response({"error": "Not found"}, status=status.HTTP_404_NOT_FOUND)
    if request.method == "DELETE":
        syn.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    try:
        syn = _apply(syn, request.data or {}, request.user, creating=False)
    except ValueError as exc:
        return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    return Response(_row(syn))
