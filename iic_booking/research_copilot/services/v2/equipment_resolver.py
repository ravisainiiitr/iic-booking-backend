"""Deterministic equipment name resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from django.db.models import Q

from iic_booking.research_copilot.services.v2.equipment_aliases import EQUIPMENT_ALIASES


@dataclass
class EquipmentCandidate:
    id: int
    name: str
    code: str = ""
    url: str = ""


@dataclass
class EquipmentResolution:
    confidence: str  # EXACT | ALIAS | CONTEXTUAL | AMBIGUOUS | NOT_FOUND
    equipment_id: int | None = None
    equipment_name: str | None = None
    candidates: list[EquipmentCandidate] = field(default_factory=list)
    query: str = ""


def _qs_visible(user=None):
    from iic_booking.equipment.models import Equipment

    try:
        from iic_booking.equipment.api_views import get_visible_equipment_queryset

        if user is not None:
            return get_visible_equipment_queryset(user)
    except Exception:  # noqa: BLE001
        pass
    return Equipment.objects.all()


def resolve_equipment(*, text: str, user=None, context_equipment_id: int | None = None) -> EquipmentResolution:
    raw = (text or "").strip()
    lower = raw.lower()
    if len(lower) < 2:
        return EquipmentResolution(confidence="NOT_FOUND", query=raw)

    qs = _qs_visible(user)

    # Exact name/code
    exact = qs.filter(Q(name__iexact=raw) | Q(code__iexact=raw)).first()
    if exact:
        return EquipmentResolution(
            confidence="EXACT",
            equipment_id=exact.id,
            equipment_name=exact.name,
            candidates=[EquipmentCandidate(exact.id, exact.name, getattr(exact, "code", "") or "", f"/equipments/{exact.id}")],
            query=raw,
        )

    # Alias hits
    needles: list[str] = []
    for alias, alias_needles in EQUIPMENT_ALIASES.items():
        if alias in lower or any(n in lower for n in alias_needles):
            needles.extend([alias, *alias_needles])
    # Also treat free tokens as needles
    for tok in ("fesem", "sem", "tem", "xrd", "pxrd", "afm", "xps", "icp", "eds"):
        if tok in lower and tok not in needles:
            needles.append(tok)

    hits: dict[int, EquipmentCandidate] = {}
    if needles:
        filt = Q()
        for n in needles:
            filt |= Q(name__icontains=n) | Q(code__icontains=n) | Q(description__icontains=n)
        for eq in qs.filter(filt).order_by("name")[:8]:
            hits[eq.id] = EquipmentCandidate(eq.id, eq.name, getattr(eq, "code", "") or "", f"/equipments/{eq.id}")

    # Substring name search on remaining words
    if not hits:
        words = [w for w in lower.replace(",", " ").split() if len(w) >= 3 and w not in {"the", "for", "and", "this", "week", "next", "slot", "slots", "find", "search", "available", "availability"}]
        if words:
            filt = Q()
            for w in words[:4]:
                filt |= Q(name__icontains=w) | Q(code__icontains=w)
            for eq in qs.filter(filt).order_by("name")[:8]:
                hits[eq.id] = EquipmentCandidate(eq.id, eq.name, getattr(eq, "code", "") or "", f"/equipments/{eq.id}")

    cands = list(hits.values())
    if len(cands) == 1:
        c = cands[0]
        return EquipmentResolution(
            confidence="ALIAS" if needles else "EXACT",
            equipment_id=c.id,
            equipment_name=c.name,
            candidates=cands,
            query=raw,
        )
    if len(cands) > 1:
        return EquipmentResolution(confidence="AMBIGUOUS", candidates=cands, query=raw)

    if context_equipment_id:
        eq = qs.filter(pk=context_equipment_id).first()
        if eq:
            return EquipmentResolution(
                confidence="CONTEXTUAL",
                equipment_id=eq.id,
                equipment_name=eq.name,
                candidates=[EquipmentCandidate(eq.id, eq.name, getattr(eq, "code", "") or "", f"/equipments/{eq.id}")],
                query=raw,
            )

    return EquipmentResolution(confidence="NOT_FOUND", query=raw)
