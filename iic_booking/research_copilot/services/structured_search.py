"""Structured portal data search (read-only) — Phase AI.2."""

from __future__ import annotations

from dataclasses import dataclass

import re

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

    # Prefer instrument-family tokens when the user asks a natural-language question
    # (e.g. "How much does 5 XRD samples cost?").
    known = (
        "xrd", "pxrd", "fesem", "sem", "tem", "afm", "xps", "raman", "ftir",
        "nmr", "gcms", "lcms", "icp", "bet", "saxs", "waxs", "eds", "edx",
    )
    lower = q.lower()
    tokens = [t for t in re.findall(r"[a-zA-Z0-9]{2,}", lower) if t not in {
        "how", "much", "does", "the", "for", "and", "what", "when", "where", "which",
        "can", "i", "my", "is", "are", "of", "to", "a", "an", "in", "on", "with",
        "sample", "samples", "cost", "price", "charge", "fee", "available", "slots",
        "software", "analysis", "booking", "book", "tomorrow", "today", "should",
        "prepare", "coming", "difference", "between",
    }]
    preferred = [t for t in tokens if t in known] or tokens[:4] or [q]

    filt = Q()
    for token in preferred:
        part = Q(name__icontains=token) | Q(description__icontains=token)
        if any(f.name == "code" for f in Equipment._meta.get_fields()):
            part = part | Q(code__icontains=token)
        filt |= part

    qs = Equipment.objects.filter(filt).order_by("name")[:limit]
    hits: list[StructuredHit] = []
    for eq in qs:
        eq_pk = int(eq.pk)
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
                source_id=f"equipment:{eq_pk}",
                title=eq.name,
                snippet=" | ".join(p for p in snippet_parts if p),
                score=0.72,
                url=f"/equipments/{eq_pk}",
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
