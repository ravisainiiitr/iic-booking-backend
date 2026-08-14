"""Structured portal data search (read-only) — Phase AI.2 / AI.22.1 ranking."""

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
    family: str = ""


def _equipment_family(name: str) -> str:
    n = (name or "").lower()
    if "gi-xrd" in n or "gi xrd" in n or "grazing" in n:
        return "gi-xrd"
    if "pxrd" in n or "powder" in n:
        return "pxrd"
    if "xrd" in n or "x-ray diffract" in n or "xray diffract" in n:
        return "xrd-generic"
    if "fesem" in n or "fe-sem" in n or "fe sem" in n:
        return "fesem"
    if re.search(r"\bsem\b", n) and "fesem" not in n and "fe-sem" not in n:
        return "sem"
    if re.search(r"\btem\b", n):
        return "tem"
    if "afm" in n:
        return "afm"
    if "xps" in n:
        return "xps"
    return "other"


def _query_xrd_intent(lower: str) -> str:
    """Return pxrd | gi-xrd | ambiguous | none for XRD-family queries."""
    if "gi-xrd" in lower or "gi xrd" in lower or "grazing" in lower:
        return "gi-xrd"
    if "pxrd" in lower or re.search(r"\bpowder\b", lower):
        return "pxrd"
    if re.search(r"\bxrd\b", lower) or "x-ray diffract" in lower or "xray diffract" in lower:
        return "ambiguous"
    return "none"


def score_equipment_match(*, query: str, name: str, code: str = "") -> tuple[float, str]:
    """Deterministic ranking score for an equipment row against a user query (AI.22.1)."""
    lower = (query or "").lower()
    title = (name or "").lower()
    code_l = (code or "").lower()
    family = _equipment_family(f"{name} {code}")
    score = 0.45

    # Exact / strong token presence in name or code
    tokens = re.findall(r"[a-z0-9]{2,}", lower)
    for tok in tokens:
        if tok in {"how", "much", "does", "cost", "samples", "sample", "available", "slots"}:
            continue
        if tok and (tok in title or tok in code_l):
            score = max(score, 0.7)

    intent = _query_xrd_intent(lower)
    if intent == "pxrd":
        score = 0.98 if family == "pxrd" else (0.25 if family == "gi-xrd" else score)
    elif intent == "gi-xrd":
        score = 0.98 if family == "gi-xrd" else (0.25 if family == "pxrd" else score)
    elif intent == "ambiguous":
        if family in {"pxrd", "gi-xrd", "xrd-generic"}:
            score = max(score, 0.82)

    if "fesem" in lower or "fe-sem" in lower:
        score = 0.97 if family == "fesem" else score
    if re.search(r"\bsem\b", lower) and "fesem" not in lower and "fe-sem" not in lower:
        if family == "sem":
            score = max(score, 0.9)
        elif family == "fesem":
            score = max(score, 0.75)

    # Prefer shorter / primary instrument labels slightly within same family
    if "[" in title and family in {"pxrd", "fesem"}:
        score -= 0.01
    return round(min(score, 0.99), 3), family


def xrd_family_clarification(*, text: str, hits: list[StructuredHit] | None = None) -> str | None:
    """When bare XRD is used for a single-instrument action, ask PXRD vs GI-XRD."""
    lower = (text or "").lower()
    if _query_xrd_intent(lower) != "ambiguous":
        return None
    families = {getattr(h, "family", "") or _equipment_family(h.title) for h in (hits or [])}
    if "pxrd" in families and "gi-xrd" in families:
        return "Do you mean PXRD (powder XRD) or GI-XRD (grazing incidence XRD)?"
    return None


def search_equipment(*, query: str, limit: int = 5) -> list[StructuredHit]:
    from iic_booking.equipment.models import Equipment

    q = (query or "").strip()
    if len(q) < 2:
        return []

    # Prefer instrument-family tokens when the user asks a natural-language question
    # (e.g. "How much does 5 XRD samples cost?").
    known = (
        "xrd",
        "pxrd",
        "fesem",
        "sem",
        "tem",
        "afm",
        "xps",
        "raman",
        "ftir",
        "nmr",
        "gcms",
        "lcms",
        "icp",
        "bet",
        "saxs",
        "waxs",
        "eds",
        "edx",
        "powder",
        "grazing",
    )
    lower = q.lower()
    tokens = [
        t
        for t in re.findall(r"[a-zA-Z0-9]{2,}", lower)
        if t
        not in {
            "how",
            "much",
            "does",
            "the",
            "for",
            "and",
            "what",
            "when",
            "where",
            "which",
            "can",
            "i",
            "my",
            "is",
            "are",
            "of",
            "to",
            "a",
            "an",
            "in",
            "on",
            "with",
            "sample",
            "samples",
            "cost",
            "price",
            "charge",
            "fee",
            "available",
            "slots",
            "software",
            "analysis",
            "booking",
            "book",
            "tomorrow",
            "today",
            "should",
            "prepare",
            "coming",
            "difference",
            "between",
        }
    ]
    # Keep multi-word GI/PXRD cues
    if "gi-xrd" in lower or "gi xrd" in lower:
        preferred = ["gi-xrd", "grazing", "xrd"]
    elif "pxrd" in lower or "powder" in lower:
        preferred = ["pxrd", "powder", "xrd"]
    else:
        preferred = [t for t in tokens if t in known] or tokens[:4] or [q]

    filt = Q()
    for token in preferred:
        tok = token.replace("-", "")
        part = Q(name__icontains=token) | Q(description__icontains=token)
        if token == "gi-xrd":
            part = part | Q(name__icontains="grazing") | Q(name__icontains="GI-XRD")
        if token == "pxrd":
            part = part | Q(name__icontains="powder") | Q(name__icontains="PXRD")
        if any(f.name == "code" for f in Equipment._meta.get_fields()):
            part = part | Q(code__icontains=token)
        if tok != token:
            part = part | Q(name__icontains=tok)
        filt |= part

    # Fetch a wider candidate set then rank (avoid alphabetical GI-XRD bias).
    qs = list(Equipment.objects.filter(filt)[:40])
    hits: list[StructuredHit] = []
    for eq in qs:
        eq_pk = int(eq.pk)
        code = getattr(eq, "code", "") or ""
        score, family = score_equipment_match(query=q, name=eq.name or "", code=code)
        # Drop weak cross-family matches for specific intents
        intent = _query_xrd_intent(lower)
        if intent == "pxrd" and family == "gi-xrd" and score < 0.5:
            continue
        if intent == "gi-xrd" and family == "pxrd" and score < 0.5:
            continue
        snippet_parts = [
            (eq.description or "")[:160],
            f"DSA={'yes' if getattr(eq, 'dsa_enabled', False) else 'no'}",
            f"RemoteAnalysis={'yes' if getattr(eq, 'enable_remote_analysis', False) else 'no'}",
        ]
        loc = (getattr(eq, "location", None) or "").strip()
        if loc:
            snippet_parts.append(f"Location: {loc}")
        hits.append(
            StructuredHit(
                source_id=f"equipment:{eq_pk}",
                title=eq.name,
                snippet=" | ".join(p for p in snippet_parts if p)[:280],
                score=score,
                url=f"/equipments/{eq_pk}",
                category="equipment",
                family=family,
            )
        )
    hits.sort(key=lambda h: (-h.score, h.title or ""))
    return hits[:limit]


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
