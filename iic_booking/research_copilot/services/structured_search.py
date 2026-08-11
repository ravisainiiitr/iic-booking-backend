"""Structured portal data search (read-only) — Phase AI.2."""

from __future__ import annotations

from dataclasses import dataclass

from django.db.models import Q


@dataclass
class StructuredHit:
    source_id: str
    title: str
    snippet: str
    score: float
    url: str
    category: str


def search_equipment(*, query: str, limit: int = 5) -> list[StructuredHit]:
    from iic_booking.equipment.models import Equipment

    q = (query or "").strip()
    if len(q) < 2:
        return []

    filt = Q(name__icontains=q) | Q(description__icontains=q)
    if any(f.name == "code" for f in Equipment._meta.get_fields()):
        filt = filt | Q(code__icontains=q)

    qs = Equipment.objects.filter(filt).order_by("name")[:limit]
    hits: list[StructuredHit] = []
    for eq in qs:
        specs = list(eq.equipment_specifications.all()[:6])
        spec_txt = "; ".join(f"{s.spec_key}: {s.spec_value}" for s in specs) if specs else ""
        accessories = [a.accessory_name for a in eq.equipment_accessories.all()[:5]]
        snippet_parts = [
            (eq.description or "")[:240],
            f"DSA={'yes' if getattr(eq, 'dsa_enabled', False) else 'no'}",
            f"RemoteAnalysis={'yes' if getattr(eq, 'enable_remote_analysis', False) else 'no'}",
        ]
        loc = (getattr(eq, "location", None) or "").strip()
        if loc:
            snippet_parts.append(f"Location: {loc}")
        if spec_txt:
            snippet_parts.append(f"Specs: {spec_txt}")
        if accessories:
            snippet_parts.append("Accessories: " + ", ".join(accessories))
        hits.append(
            StructuredHit(
                source_id=f"equipment:{eq.id}",
                title=eq.name,
                snippet=" | ".join(p for p in snippet_parts if p),
                score=0.72,
                url=f"/equipments/{eq.id}",
                category="equipment",
            )
        )
    return hits


def search_booking_statuses(*, query: str) -> list[StructuredHit]:
    catalog = [
        ("SAMPLE_ACCEPTED", "Operator has accepted the physical/virtual sample; experiment can proceed."),
        ("HOLD", "Booking or sample is on hold pending clarification, payment, or operator action."),
        ("COMPLETED", "Experiment finished; results may be available for download."),
        ("CANCELLED", "Booking was cancelled per policy; wallet rules may apply."),
        ("PENDING", "Awaiting approval or operator action."),
    ]
    q = (query or "").lower()
    hits = []
    for code, meaning in catalog:
        if code.lower() in q or any(w in q for w in ("status", "sample", "hold", "accepted", "result")):
            hits.append(
                StructuredHit(
                    source_id=f"status:{code}",
                    title=f"Status: {code}",
                    snippet=meaning,
                    score=0.8 if code.lower() in q else 0.55,
                    url="/bookings",
                    category="policy",
                )
            )
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:3]


def structured_search(*, query: str, intent: str, limit: int = 5) -> list[StructuredHit]:
    hits: list[StructuredHit] = []
    if intent in {"equipment", "general", "documentation"}:
        hits.extend(search_equipment(query=query, limit=limit))
    if intent in {"status", "policy", "general"}:
        hits.extend(search_booking_statuses(query=query))
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
