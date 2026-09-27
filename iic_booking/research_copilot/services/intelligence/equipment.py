"""Equipment lookups for the intelligence layer: real portal rows under the user's visibility."""

from __future__ import annotations

import re
from typing import Any

from django.db.models import Case, IntegerField, Q, Value, When

from iic_booking.research_copilot.services.intelligence import terminology
from iic_booking.research_copilot.services.v2.equipment_resolver import EquipmentResolution, _qs_visible, resolve_equipment

# Words that describe the request, not the instrument; stripped before name matching so
# "book xrd tomorrow morning" never matches an instrument whose name contains "book" or "morning".
_REQUEST_WORDS = {
    "book", "booking", "bookings", "reserve", "slot", "slots", "tomorrow", "today", "tonight", "week", "next",
    "this", "morning", "afternoon", "evening", "cost", "costs", "price", "pricing", "estimate", "charge",
    "charges", "rate", "rates", "for", "sample", "samples", "cancel", "available", "availability", "earliest",
    "first", "check", "show", "find", "list", "search", "me", "i", "want", "need", "to", "a", "an", "the",
    "please", "how", "much", "what", "is", "are", "of", "on", "in", "at", "my", "can", "do", "does", "about",
    "tell", "details", "detail", "info", "information", "equipment", "equipments", "instrument", "instruments",
    "machine", "facility", "compare", "and", "vs", "versus", "with", "which", "use", "using", "would", "like",
    "get", "give", "run", "measure", "analysis", "slot?", "when", "free", "open", "view", "see", "any", "some",
    "all", "other", "more", "specs", "specifications", "where", "located", "location", "who", "oic",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "after", "before", "am", "pm",
}


def visible_queryset(user):
    return _qs_visible(user)


def equipment_query(text: str) -> str:
    lower = terminology.normalize(text)
    lower = re.sub(r"\b\d{1,3}\s*(samples?|specimens?|hours?|hrs?|mins?|minutes?)\b", " ", lower)
    words = [w for w in re.split(r"[\s,?!.;:]+", lower) if w and w not in _REQUEST_WORDS and not w.isdigit()]
    return " ".join(words).strip()


def resolve(*, user, text: str, context_equipment_id: int | None = None) -> EquipmentResolution:
    query = equipment_query(text)
    if len(query) < 2:
        if context_equipment_id:
            return _contextual(user, context_equipment_id)
        return EquipmentResolution(confidence="NOT_FOUND", query=query)
    return resolve_equipment(text=query, user=user, context_equipment_id=context_equipment_id)


def _contextual(user, equipment_id: int) -> EquipmentResolution:
    from iic_booking.research_copilot.services.v2.equipment_resolver import EquipmentCandidate

    eq = visible_queryset(user).filter(pk=equipment_id).first()
    if not eq:
        return EquipmentResolution(confidence="NOT_FOUND")
    return EquipmentResolution(
        confidence="CONTEXTUAL",
        equipment_id=int(eq.pk),
        equipment_name=eq.name,
        candidates=[EquipmentCandidate(int(eq.pk), eq.name, eq.code or "", f"/equipment/{eq.pk}")],
    )


def get_visible(user, equipment_id) -> Any:
    try:
        return visible_queryset(user).select_related("internal_department").filter(pk=int(equipment_id)).first()
    except (TypeError, ValueError):
        return None


def row(eq) -> dict[str, Any]:
    from iic_booking.equipment.models import EquipmentStatus

    status = (eq.status or "").strip()
    dept = getattr(eq, "internal_department", None)
    return {
        "id": int(eq.pk),
        "name": eq.name,
        "code": eq.code or "",
        "department": getattr(dept, "name", "") or "",
        "location": (eq.location or "").strip()[:160],
        "status": status,
        "status_label": eq.get_status_display() if status else "Unknown",
        "bookable": status == EquipmentStatus.ACTIVE,
        "href": f"/equipment/{eq.pk}",
    }


def search(*, user, technique_keys: list[str], text: str = "", limit: int = 6, offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Visible equipment matching technique needles (name/code first, description as fallback)."""
    from iic_booking.equipment.models import EquipmentStatus

    qs = visible_queryset(user).select_related("internal_department")
    needles: list[str] = []
    for key in technique_keys:
        tech = terminology.TECHNIQUES.get(key)
        if tech:
            needles.extend([tech.key, *tech.needles])
    query = equipment_query(text) if not needles else ""
    if query:
        needles.extend([w for w in query.split() if len(w) >= 3][:4])
    if not needles:
        return [], 0
    filt = Q()
    for n in dict.fromkeys(needles):
        if len(n) <= 4:
            filt |= Q(name__iregex=rf"(^|[^a-z0-9]){re.escape(n)}([^a-z0-9]|$)") | Q(code__icontains=n)
        else:
            filt |= Q(name__icontains=n) | Q(code__icontains=n)
    matched = qs.filter(filt)
    if not matched.exists():
        desc = Q()
        for n in dict.fromkeys(needles):
            if len(n) >= 3:
                desc |= Q(description__icontains=n)
        matched = qs.filter(desc) if desc else qs.none()
    matched = matched.annotate(
        _active=Case(When(status=EquipmentStatus.ACTIVE, then=Value(0)), default=Value(1), output_field=IntegerField())
    ).order_by("_active", "name")
    total = matched.count()
    return [row(eq) for eq in matched[offset : offset + limit]], total


def techniques_with_equipment(user, keys: list[str]) -> list[str]:
    """Subset of technique keys that match at least one visible instrument (keeps choice lists honest)."""
    out = []
    for key in keys:
        rows, total = search(user=user, technique_keys=[key], limit=1)
        if total:
            out.append(key)
    return out
