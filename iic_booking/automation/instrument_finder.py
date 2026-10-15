"""
Instrument finder: a researcher describes the sample and goal in plain words ("I want to know the crystal structure
of my thin film") and gets ranked instruments with reasons.

Rules first, always: technique names and research purposes from the Copilot terminology table, goal and sample
phrases below, and the admin-managed search synonyms (technique -> categories / instruments). Only when the rules
find no technique, and the admin switched it on, the configured AI model is asked to pick technique keys from the
same whitelist (capped per day, request text only). Every instrument comes from the equipment the user may see.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from iic_booking.research_copilot.services.intelligence import terminology

MAX_RESULTS = 8
MAX_QUERY_CHARS = 400

# Plain-language goals -> techniques, with the sentence shown as the reason.
GOALS: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (r"crystal(line)? structure|crystallin|crystal phase|phases? present|which phase|phase (purity|composition|identification)|lattice|crystallite|amorphous or crystalline|d-?spacing",
     ("xrd", "pxrd", "tem"), "crystal structure and phases"),
    (r"surface (look|morpholog|feature|texture)|morpholog|microstructure|grain size|particle shape|cracks?|fracture|porosity image|see (the )?(surface|particles?|grains?)",
     ("fesem", "sem", "afm"), "surface morphology and microstructure"),
    (r"nanoparticle size|size of (my )?nanoparticles?|lattice fringes|atomic (resolution|structure)|inside (the )?(particle|sample)|internal structure|core.?shell",
     ("tem",), "nanoscale internal structure and particle size"),
    (r"rough(ness)?|topograph|surface height|step height|film thickness|thickness of (my )?(film|coating|layer)",
     ("afm", "profilometer"), "surface roughness, topography and thickness"),
    (r"elements? (present|in)|which elements|elemental|composition|impurit|contaminat|stoichiometr|doping level|dopant",
     ("eds", "xps", "icp"), "elemental composition"),
    (r"oxidation state|chemical state|valence|surface chemistry|binding energy",
     ("xps",), "surface chemical and oxidation states"),
    (r"trace (metal|element)s?|heavy metals?|ppm|ppb|metal (ion )?concentration|concentration of (metal|ions?)",
     ("icp",), "trace element concentration"),
    (r"functional groups?|chemical bonds?|bonding|organic groups?|polymer (structure|identification)|identify (the )?(polymer|compound)",
     ("ftir", "raman", "nmr"), "functional groups and chemical bonding"),
    (r"graphene|carbon nanotubes?|cnts?|d and g band|defects in carbon|number of layers",
     ("raman",), "carbon materials and layer count"),
    (r"band ?gap|absorb(ance|ption)|optical propert|colou?r|transmittance",
     ("uv",), "optical absorption and band gap"),
    (r"surface area|pore size|porosity|porous|adsorption isotherm",
     ("bet",), "surface area and porosity"),
    (r"thermal stability|decompos|weight loss|mass loss|moisture content|ash content|burn",
     ("tga",), "thermal stability and mass loss"),
    (r"melting( point)?|glass transition|\btg\b|crystalli[sz]ation temperature|heat flow|enthalpy",
     ("dsc",), "melting, glass transition and heat flow"),
    (r"magneti[cz]|hysteresis|coercivity|saturation magneti",
     ("vsm",), "magnetic properties"),
    (r"radicals?|unpaired electrons?|paramagnetic",
     ("epr",), "radicals and unpaired electrons"),
    (r"molecular structure|structure of (my )?(molecule|compound)|structure elucidation|purity of (my )?(compound|product)|organic synthesis",
     ("nmr", "ftir"), "molecular structure"),
    (r"particle size distribution|hydrodynamic|zeta|dispersion stability|colloid",
     ("dls",), "particle size in liquid and zeta potential"),
    (r"fluorescen|live cells?|cell imaging|tissue|3d imaging of cells",
     ("confocal",), "fluorescence and cell imaging"),
    (r"3d print|prototype|print (a|my) (part|model)|stl",
     ("printer",), "3D printed parts"),
)

# Sample descriptions -> techniques that usually suit them (a small boost, or suggestions when no goal is given).
SAMPLES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    (r"thin films?|coatings?|deposited layers?|on (a )?(silicon|glass|substrate)|wafer", ("xrd", "afm", "fesem", "xps", "profilometer"), "thin film"),
    (r"powders?|nanopowder|calcined", ("pxrd", "xrd", "sem", "bet", "tga"), "powder"),
    (r"solutions?|liquids?|aqueous|water samples?|effluent|digested", ("icp", "uv", "dls", "nmr"), "liquid / solution"),
    (r"polymers?|plastics?|resin|rubber|composite", ("ftir", "dsc", "tga", "sem"), "polymer"),
    (r"nanoparticles?|quantum dots?|nanorods?|nanowires?", ("tem", "dls", "xrd", "uv"), "nanoparticles"),
    (r"metals?|alloys?|steel|welds?", ("sem", "eds", "xrd"), "metal / alloy"),
    (r"biological|cells?|bacteria|tissue|protein", ("confocal", "uv", "dls"), "biological sample"),
    (r"catalysts?|zeolites?|mofs?|activated carbon", ("bet", "xrd", "xps", "tem"), "catalyst / porous solid"),
)

_SAMPLE_BOOST = 0.08


@dataclass
class TechniqueHit:
    key: str
    score: float
    why: str
    source: str  # named | goal | purpose | sample | synonym | ai


@dataclass
class FinderResult:
    query: str
    techniques: list[TechniqueHit] = field(default_factory=list)
    sample: str = ""
    method: str = "rules"
    synonym_equipment: dict[int, tuple[float, str]] = field(default_factory=dict)


def _norm(text: str) -> str:
    return terminology.normalize(text)[:MAX_QUERY_CHARS]


def _add(hits: dict[str, TechniqueHit], key: str, score: float, why: str, source: str) -> None:
    if key not in terminology.TECHNIQUES:
        return
    cur = hits.get(key)
    if cur is None or score > cur.score:
        hits[key] = TechniqueHit(key, score, why, source)


def _synonym_hits(lower: str, hits: dict[str, TechniqueHit], out: FinderResult) -> None:
    """Admin search synonyms: a term in the text points at a technique's categories and instruments."""
    try:
        from iic_booking.equipment.models import SearchSynonym
    except ImportError:
        return
    try:
        rows = list(SearchSynonym.objects.filter(is_active=True).prefetch_related("equipment", "categories")[:500])
    except Exception:  # noqa: BLE001
        return
    for syn in rows:
        terms = [t for t in syn.term_list() if len(t) >= 3]
        hit = next((t for t in terms if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", lower)), None)
        if not hit:
            continue
        why = f"\u201c{hit}\u201d is a search term for {syn.technique}"
        for t in terminology.find_techniques(f"{syn.technique} {' '.join(terms)}"):
            _add(hits, t.key, 0.8, why, "synonym")
        for eq in syn.equipment.all():
            cur = out.synonym_equipment.get(int(eq.pk))
            if cur is None or cur[0] < 0.82:
                out.synonym_equipment[int(eq.pk)] = (0.82, why)
        cat_ids = [c.pk for c in syn.categories.all()]
        if cat_ids:
            from iic_booking.equipment.models import Equipment

            for pk in Equipment.objects.filter(category_id__in=cat_ids).values_list("pk", flat=True)[:200]:
                out.synonym_equipment.setdefault(int(pk), (0.7, why))


def analyse(text: str, *, use_llm: bool = False, actor=None) -> FinderResult:
    lower = _norm(text)
    out = FinderResult(query=(text or "").strip()[:MAX_QUERY_CHARS])
    hits: dict[str, TechniqueHit] = {}
    if not lower:
        return out
    for t in terminology.find_techniques(lower):
        _add(hits, t.key, 1.0, f"You mentioned {t.label}", "named")
    for pattern, keys, goal in GOALS:
        if re.search(pattern, lower):
            for i, key in enumerate(keys):
                label = terminology.TECHNIQUES[key].label.split(" (")[0] if key in terminology.TECHNIQUES else key
                _add(hits, key, 0.9 - 0.05 * i, f"{label} is used for {goal}", "goal")
    for t in terminology.techniques_for_purpose(lower):
        _add(hits, t.key, 0.75, t.summary or t.label, "purpose")
    _synonym_hits(lower, hits, out)

    sample_keys: tuple[str, ...] = ()
    for pattern, keys, label in SAMPLES:
        if re.search(pattern, lower):
            out.sample = label
            sample_keys = keys
            break
    if hits and sample_keys:
        for key in sample_keys:
            if key in hits:
                hits[key].score = min(1.0, hits[key].score + _SAMPLE_BOOST)
                hits[key].why += f"; suits a {out.sample}"
    elif sample_keys:
        for i, key in enumerate(sample_keys):
            label = terminology.TECHNIQUES[key].label.split(" (")[0]
            _add(hits, key, 0.6 - 0.03 * i, f"{label} is commonly used on a {out.sample}", "sample")

    if not hits and use_llm:
        from .audit import classify_with_llm

        choices = {k: t.label for k, t in terminology.TECHNIQUES.items()}
        picked = classify_with_llm(text, choices, purpose="instrument finder", actor=actor)
        for i, key in enumerate(picked):
            _add(hits, key, 0.65 - 0.05 * i, f"Suggested by the AI model: {terminology.TECHNIQUES[key].label}", "ai")
        if picked:
            out.method = "rules+ai"
    out.techniques = sorted(hits.values(), key=lambda h: (-h.score, h.key))
    return out


def _needles(key: str) -> set[str]:
    from iic_booking.research_copilot.services.assistant.matching import _NEEDLES

    return set(_NEEDLES.get(key, ()))


def find_instruments(user, text: str, *, use_llm: bool = False, limit: int = MAX_RESULTS) -> dict[str, Any]:
    from iic_booking.research_copilot.services.assistant import matching

    res = analyse(text, use_llm=use_llm, actor=user)
    tech_scores = {h.key: h for h in res.techniques}
    ranked: dict[int, dict[str, Any]] = {}
    pool = matching.visible_pool(user) if (tech_scores or res.synonym_equipment) else []
    for f in pool:
        pk = int(f.eq.pk)
        best = 0.0
        reasons: list[str] = []
        matched: list[str] = []
        for key, hit in tech_scores.items():
            if key in f.techniques:
                score = hit.score
            elif any(n and (n in f.name_c or n in f.description.replace(" ", "")) for n in _needles(key)):
                score = hit.score - 0.15
            else:
                continue
            matched.append(key)
            if hit.why not in reasons:
                reasons.append(hit.why)
            best = max(best, score)
        syn = res.synonym_equipment.get(pk)
        if syn:
            best = max(best, syn[0])
            if syn[1] not in reasons:
                reasons.append(syn[1])
        if best <= 0:
            continue
        best += 0.03 * max(0, len(matched) - 1)
        if not f.active:
            best -= 0.3
        ranked[pk] = {"f": f, "score": round(min(best, 1.0), 3), "reasons": reasons[:3], "techniques": matched}
    rows = [r for r in ranked.values() if r["score"] >= 0.3]
    rows.sort(key=lambda r: (-r["score"], 1 if getattr(r["f"].eq, "parent_equipment_id", None) else 0, r["f"].eq.name or ""))
    seen_parents: set[int] = set()
    results: list[dict[str, Any]] = []
    for r in rows:
        eq = r["f"].eq
        parent = getattr(eq, "parent_equipment_id", None)
        if parent and int(parent) in ranked:
            continue
        if int(eq.pk) in seen_parents:
            continue
        seen_parents.add(int(eq.pk))
        base = matching.option_row(matching.Candidate(eq, r["score"], "capability"))
        results.append(
            {
                **base,
                "score": r["score"],
                "reasons": r["reasons"],
                "techniques": [terminology.TECHNIQUES[k].label for k in r["techniques"] if k in terminology.TECHNIQUES],
                "detail_href": f"/equipment/{int(eq.pk)}",
                "book_href": f"/book-equipment?equipment_id={int(eq.pk)}",
            }
        )
        if len(results) >= limit:
            break
    techniques = [
        {
            "key": h.key,
            "label": terminology.TECHNIQUES[h.key].label,
            "summary": terminology.TECHNIQUES[h.key].summary,
            "why": h.why,
            "source": h.source,
            "score": round(h.score, 3),
            "available": any(h.key in r["techniques"] for r in rows),
        }
        for h in res.techniques[:6]
    ]
    tips: list[str] = []
    if not res.techniques:
        tips.append(
            "Describe what you want to learn about the sample (for example crystal structure, surface morphology, "
            "elements present, band gap, thermal stability) and what the sample is (thin film, powder, solution)."
        )
    elif not results:
        tips.append("No instrument on the portal that your account can book offers these techniques. Raise a support ticket for advice.")
    return {
        "query": res.query,
        "sample": res.sample,
        "method": res.method,
        "techniques": techniques,
        "results": results,
        "tips": tips,
    }
