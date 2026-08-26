"""Deterministic equipment name resolution."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

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


def _token_in_text(token: str, text: str) -> bool:
    """Word-ish match so 'sem' does not match inside 'fesem'."""
    t = (token or "").strip().lower()
    if not t:
        return False
    if " " in t or "-" in t:
        return t in text
    return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", text) is not None


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
        eid = int(exact.pk)
        return EquipmentResolution(
            confidence="EXACT",
            equipment_id=eid,
            equipment_name=exact.name,
            candidates=[EquipmentCandidate(eid, exact.name, getattr(exact, "code", "") or "", f"/equipments/{eid}")],
            query=raw,
        )

    # Alias hits — token-aware so FESEM does not activate bare SEM.
    needles: list[str] = []
    matched_aliases: list[str] = []
    for alias, alias_needles in EQUIPMENT_ALIASES.items():
        if _token_in_text(alias, lower) or any(_token_in_text(n, lower) for n in alias_needles):
            matched_aliases.append(alias)
            needles.extend([alias, *alias_needles])

    # If FESEM matched, drop bare SEM needles (too broad vs FE-SEM catalog).
    if "fesem" in matched_aliases:
        needles = [n for n in needles if n not in {"sem", "scanning electron"}]
        matched_aliases = [a for a in matched_aliases if a != "sem"]

    # Deduplicate needles preserving order
    seen: set[str] = set()
    uniq: list[str] = []
    for n in needles:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    needles = uniq

    hits: dict[int, EquipmentCandidate] = {}
    if needles:
        # Prefer name/code matches over description to avoid noisy substring hits
        filt = Q()
        for n in needles:
            filt |= Q(name__icontains=n) | Q(code__icontains=n)
        for eq in qs.filter(filt).order_by("name")[:12]:
            eid = int(eq.pk)
            hits[eid] = EquipmentCandidate(eid, eq.name, getattr(eq, "code", "") or "", f"/equipments/{eid}")
        if not hits:
            filt = Q()
            for n in needles:
                filt |= Q(description__icontains=n)
            for eq in qs.filter(filt).order_by("name")[:8]:
                eid = int(eq.pk)
                hits[eid] = EquipmentCandidate(eid, eq.name, getattr(eq, "code", "") or "", f"/equipments/{eid}")

    # FESEM queries: keep Field Emission / FE-SEM instruments only when possible
    if len(hits) > 1 and ("fesem" in matched_aliases or "fesem" in lower or "fe-sem" in lower):
        narrowed = {
            k: v
            for k, v in hits.items()
            if "fesem" in v.name.lower().replace("-", "").replace(" ", "")
            or "fe-sem" in v.name.lower()
            or "field emission scanning" in v.name.lower()
        }
        if narrowed:
            hits = narrowed

    # Substring name search on remaining words
    if not hits:
        words = [w for w in lower.replace(",", " ").split() if len(w) >= 3 and w not in {"the", "for", "and", "this", "week", "next", "slot", "slots", "find", "search", "available", "availability"}]
        if words:
            filt = Q()
            for w in words[:4]:
                filt |= Q(name__icontains=w) | Q(code__icontains=w)
            for eq in qs.filter(filt).order_by("name")[:8]:
                eid = int(eq.pk)
                hits[eid] = EquipmentCandidate(eid, eq.name, getattr(eq, "code", "") or "", f"/equipments/{eid}")

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
            eid = int(eq.pk)
            return EquipmentResolution(
                confidence="CONTEXTUAL",
                equipment_id=eid,
                equipment_name=eq.name,
                candidates=[EquipmentCandidate(eid, eq.name, getattr(eq, "code", "") or "", f"/equipments/{eid}")],
                query=raw,
            )

    return EquipmentResolution(confidence="NOT_FOUND", query=raw)
