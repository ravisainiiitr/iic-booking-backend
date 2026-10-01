"""
Fuzzy equipment matching over the equipment the user may see.

The candidate pool is always `get_visible_equipment_queryset(user)` (visibility groups, department
catalog rules, OIC/operator scoping and multi-mode catalog filtering), so a match can never reveal an
instrument the portal would hide. Names, codes, acronyms ("Field Emission Scanning Electron
Microscope" -> FESEM), parenthetical abbreviations, technique aliases (FE-SEM, ICP-MS, x-ray
diffraction), misspellings (fesm, xdr) and categories are all scored; only a single clear winner is
opened directly, everything else becomes clickable options.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from iic_booking.research_copilot.services.intelligence import terminology

MAX_OPTIONS = 6
UNIQUE_THRESHOLD = 0.9
MIN_SCORE = 0.5

EXTRA_ALIASES: dict[str, tuple[str, ...]] = {
    "fesem": ("field emission scanning electron microscope", "field emission sem", "fegsem", "feg sem", "feg-sem"),
    "sem": ("scanning electron microscope", "scanning electron microscopy"),
    "tem": ("transmission electron microscope", "hr-tem", "hr tem"),
    "xrd": ("xray diffraction", "x-ray diffractometer", "xray diffractometer"),
    "icp": ("icpms", "icp ms", "icpoes", "inductively coupled plasma"),
    "ftir": ("ft ir", "fourier transform infrared"),
    "uv": ("uvvis", "uv visible", "uv-vis-nir"),
    "afm": ("atomic force microscope",),
    "xps": ("x-ray photoelectron", "xray photoelectron"),
    "nmr": ("nuclear magnetic resonance",),
    "tga": ("thermo gravimetric", "thermogravimetry"),
}

_STOP = {
    "i", "me", "my", "we", "our", "us", "you", "your", "a", "an", "the", "this", "that", "these", "those", "it",
    "is", "are", "was", "be", "am", "do", "does", "did", "can", "could", "would", "will", "shall", "should", "may",
    "might", "must", "have", "has", "had", "need", "needs", "want", "wanted", "like", "please", "pls", "plz", "kindly",
    "book", "booking", "bookings", "reserve", "reservation", "schedule", "slot", "slots", "available", "availability",
    "free", "open", "vacant", "options", "option", "choices", "what", "whats", "which", "when", "where", "who", "whom",
    "how", "why", "any", "some", "all", "there", "here", "for", "on", "in", "at", "of", "to", "from", "with", "about",
    "and", "or", "but", "so", "if", "then", "than", "also", "just", "only", "get", "give", "show", "find", "check",
    "tell", "see", "view", "list", "search", "look", "help", "assist", "know", "let", "time", "times", "date", "day",
    "days", "earliest", "first", "next", "soon", "possible", "urgent", "urgently", "asap", "equipment", "equipments",
    "instrument", "instruments", "machine", "machines", "facility", "facilities", "lab", "tool", "system", "sample",
    "samples", "specimen", "specimens", "hour", "hours", "hrs", "minutes", "mins", "run", "use", "using", "used",
    "doing", "done", "measure", "located", "location", "locate", "address", "room", "contact",
    "contacts", "operator", "operators", "oic", "officer", "incharge", "charge", "charges", "charged", "cost", "costs",
    "price", "prices", "pricing", "rate", "rates", "fee", "fees", "tariff", "much", "many", "details", "detail",
    "info", "information", "specs", "specification", "specifications", "instruction", "instructions",
    "requirement", "requirements", "guidelines", "rules", "input", "inputs", "fields", "field", "form", "parameters",
    "status", "work", "works", "working", "operational", "slot?", "pm", "morning", "afternoon", "evening",
    "noon", "week", "weekend", "today", "tomorrow", "between", "after", "before", "around", "by", "until", "till",
    "not", "no", "yes", "ok", "okay", "hi", "hello", "hey", "thanks", "thank", "phone", "email", "number",
    "person", "people", "handles", "handle", "runs", "manages", "in-charge", "charge?", "purpose",
    "recent", "past", "previous", "last", "latest", "new", "go", "going", "make", "able", "try", "again", "now",
    "portal", "website", "site", "online", "iic", "iitr", "institute", "process", "procedure", "steps", "way",
}
_ACRONYM_SKIP = {"of", "the", "and", "for", "with", "a", "an", "in", "on", "to", "by"}
_PAREN_RE = re.compile(r"\(([^)]{2,40})\)")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", terminology.normalize(text))).strip()


def compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def osa_distance(a: str, b: str, cap: int = 3) -> int:
    """Optimal-string-alignment edit distance (adjacent transposition counts as one edit)."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] | None = None
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _technique_needles() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for key, tech in terminology.TECHNIQUES.items():
        out[key] = {compact(n) for n in (key, *tech.needles, *EXTRA_ALIASES.get(key, ())) if len(compact(n)) >= 3}
    return out


_NEEDLES = _technique_needles()


def techniques_in(text: str) -> set[str]:
    lower = terminology.normalize(text)
    keys = {t.key for t in terminology.find_techniques(lower)}
    c = compact(lower)
    for key, aliases in EXTRA_ALIASES.items():
        if any(compact(a) and compact(a) in c for a in aliases if len(compact(a)) >= 6):
            keys.add(key)
    if "fesem" in keys and "sem" in keys:
        # "field emission scanning electron microscope" names both; the narrower technique wins.
        stripped = lower
        for a in sorted((*terminology.TECHNIQUES["fesem"].needles, *EXTRA_ALIASES["fesem"]), key=len, reverse=True):
            stripped = stripped.replace(a, " ")
        if not terminology.find_techniques(stripped) or "sem" not in {t.key for t in terminology.find_techniques(stripped)}:
            keys.discard("sem")
    return keys


def misspelled_techniques(query_compact: str) -> set[str]:
    if len(query_compact) < 3:
        return set()
    hits: set[str] = set()
    for key, needles in _NEEDLES.items():
        for n in needles:
            if abs(len(n) - len(query_compact)) > 2:
                continue
            d = osa_distance(query_compact, n, cap=2)
            if d == 1 or (d == 2 and min(len(n), len(query_compact)) >= 6):
                hits.add(key)
    return hits


@dataclass
class EqFeatures:
    eq: Any
    name_c: str
    code_c: str
    code_head: str
    paren: tuple[str, ...]
    acronym: str
    tokens: frozenset[str]
    techniques: frozenset[str]
    category: str
    description: str
    active: bool
    parent_id: int | None


def features(eq) -> EqFeatures:
    from iic_booking.equipment.models import EquipmentStatus

    name = eq.name or ""
    name_n = norm(name)
    code = (eq.code or "").lower()
    code_parts = [p for p in re.split(r"[^a-z0-9]+", code) if p]
    head = code_parts[0] if code_parts else ""
    head = re.match(r"[a-z]+", head).group(0) if re.match(r"[a-z]+", head) else head
    paren = tuple(compact(p) for p in _PAREN_RE.findall(name) if compact(p))
    words = [w for w in name_n.split() if w not in _ACRONYM_SKIP]
    acronym = "".join(w[0] for w in words if w[0].isalpha()) if len(words) >= 2 else ""
    category = getattr(getattr(eq, "category", None), "name", "") or ""
    return EqFeatures(
        eq=eq,
        name_c=compact(name),
        code_c=compact(code),
        code_head=head if len(head) >= 2 else "",
        paren=paren,
        acronym=acronym if len(acronym) >= 3 else "",
        tokens=frozenset(name_n.split()),
        techniques=frozenset(techniques_in(f"{name} {code} {' '.join(_PAREN_RE.findall(name))}")),
        category=norm(category),
        description=norm(str(getattr(eq, "description", "") or ""))[:800],
        active=(eq.status or "").strip() == EquipmentStatus.ACTIVE,
        parent_id=getattr(eq, "parent_equipment_id", None),
    )


@dataclass
class Candidate:
    eq: Any
    score: float
    reason: str

    @property
    def id(self) -> int:
        return int(self.eq.pk)


@dataclass
class Match:
    status: str  # "unique" | "options" | "unavailable" | "none"
    query: str
    candidates: list[Candidate] = field(default_factory=list)
    misspelled: bool = False

    @property
    def equipment(self):
        return self.candidates[0].eq if self.status == "unique" and self.candidates else None


def _score(q_c: str, q_tokens: list[str], q_techs: set[str], q_misspelled: set[str], f: EqFeatures) -> tuple[float, str]:
    best, reason = 0.0, ""

    def bump(score: float, why: str):
        nonlocal best, reason
        if score > best:
            best, reason = score, why

    exact_targets = {f.name_c, f.code_c, *f.paren}
    if f.code_head and len(f.code_head) >= 3:
        exact_targets.add(f.code_head)
    if f.acronym:
        exact_targets.add(f.acronym)
    if q_c and q_c in exact_targets:
        bump(1.0, "exact")
    if q_techs & f.techniques:
        bump(0.93, "technique")
    elif "sem" in q_techs and "fesem" in f.techniques:
        bump(0.72, "related")
    elif "xrd" in q_techs and "pxrd" in f.techniques:
        bump(0.85, "related")
    if not q_techs and q_misspelled & f.techniques:
        bump(0.8, "spelling")
    if len(q_c) >= 3 and (q_c in f.name_c or (len(q_c) >= 4 and q_c in f.code_c)):
        bump(0.86, "name")
    if q_tokens and all(t in f.tokens for t in q_tokens):
        bump(0.88, "name")
    if best < 0.86 and len(q_c) >= 3:
        targets = [t for t in (f.name_c, f.code_c, f.code_head, f.acronym, *f.paren) if t] + [
            t for t in f.tokens if len(t) >= 3
        ]
        for t in targets:
            if len(q_c) <= 8 and abs(len(t) - len(q_c)) <= 2:
                d = osa_distance(q_c, t, cap=2)
                if d == 1 and len(q_c) >= 3:
                    bump(0.82, "spelling")
                elif d == 2 and len(q_c) >= 5:
                    bump(0.74, "spelling")
            r = SequenceMatcher(None, q_c, t).ratio()
            if r >= 0.78:
                bump(round(r * 0.9, 3), "spelling" if r < 0.97 else "name")
        long_tokens = [t for t in q_tokens if len(t) >= 3]
        if long_tokens and f.tokens:
            ratios = [max(SequenceMatcher(None, qt, nt).ratio() for nt in f.tokens) for qt in long_tokens]
            avg = sum(ratios) / len(ratios)
            if avg >= 0.8:
                bump(round(0.8 * avg, 3), "spelling" if avg < 0.97 else "name")
    sig = [t for t in q_tokens if len(t) >= 3]
    if sig and f.category and (all(t in f.category.split() for t in sig) or " ".join(sig) in f.category):
        bump(0.62, "category")
    if sig and f.description and all(re.search(rf"\b{re.escape(t)}", f.description) for t in sig if len(t) >= 4) and any(
        len(t) >= 4 for t in sig
    ):
        bump(0.55, "description")
    return best, reason


def visible_pool(user) -> list[EqFeatures]:
    from iic_booking.research_copilot.services.v2.equipment_resolver import _qs_visible

    qs = _qs_visible(user).select_related("category", "internal_department")
    return [features(eq) for eq in qs[:2000]]


def _rank(query: str, pool: list[EqFeatures]) -> tuple[list[Candidate], bool]:
    q_n = norm(query)
    q_tokens = [t for t in q_n.split() if t]
    q_c = compact(q_n)
    if len(q_c) < 2:
        return [], False
    q_techs = techniques_in(q_n)
    q_miss = misspelled_techniques(q_c) if not q_techs else set()
    scored: list[Candidate] = []
    misspelled = False
    for f in pool:
        s, why = _score(q_c, q_tokens, q_techs, q_miss, f)
        if s >= MIN_SCORE:
            scored.append(Candidate(f.eq, s, why))
    scored.sort(key=lambda c: (-c.score, 1 if getattr(c.eq, "parent_equipment_id", None) else 0, c.eq.name or ""))
    if scored and scored[0].reason == "spelling":
        misspelled = True
    return scored, misspelled


def _collapse_modes(cands: list[Candidate]) -> list[Candidate]:
    """Drop multi-mode children when their parent is also a candidate with at least the same score."""
    by_id = {c.id: c for c in cands}
    out = []
    for c in cands:
        pid = getattr(c.eq, "parent_equipment_id", None)
        parent = by_id.get(int(pid)) if pid else None
        if parent is not None and parent.score >= c.score:
            continue
        out.append(c)
    return out


def _spans(tokens: list[str]) -> list[str]:
    out = []
    for size in range(len(tokens) - 1, 0, -1):
        for i in range(0, len(tokens) - size + 1):
            out.append(" ".join(tokens[i : i + size]))
    return out


def match_equipment(user, query: str, *, pool: list[EqFeatures] | None = None) -> Match:
    query = (query or "").strip()
    if len(compact(query)) < 2:
        return Match(status="none", query=query)
    pool = pool if pool is not None else visible_pool(user)
    scored, misspelled = _rank(query, pool)
    if not scored or scored[0].score < UNIQUE_THRESHOLD:
        tokens = norm(query).split()
        if 1 < len(tokens) <= 6:
            for span in _spans(tokens):
                alt, alt_miss = _rank(span, pool)
                if alt and alt[0].score >= 0.86 and (not scored or alt[0].score > scored[0].score):
                    scored, misspelled = alt, alt_miss
                    break
    scored = _collapse_modes(scored)
    if not scored:
        return Match(status="none", query=query)
    top = scored[0]
    if not misspelled and top.score >= UNIQUE_THRESHOLD:
        runner = scored[1].score if len(scored) > 1 else 0.0
        if runner < top.score - 0.04 or (top.score >= 1.0 and runner < 1.0):
            return Match(status="unique", query=query, candidates=[top] + scored[1:MAX_OPTIONS])
    if not misspelled and len(scored) == 1 and top.score >= 0.86:
        return Match(status="unique", query=query, candidates=[top])
    active = [c for c in scored if (c.eq.status or "").strip() == "ACTIVE"]
    if not active:
        return Match(status="unavailable", query=query, candidates=scored[:MAX_OPTIONS], misspelled=misspelled)
    return Match(status="options", query=query, candidates=active[:MAX_OPTIONS], misspelled=misspelled)


def equipment_phrase(text: str) -> str:
    """The words left once request words, dates, times, numbers and question words are removed."""
    lower = norm(text)
    lower = re.sub(r"\b\d{1,3}\s*(samples?|specimens?|hours?|hrs?|mins?|minutes?|slots?|days?)\b", " ", lower)
    words = [w for w in lower.split() if w and w not in _STOP and not w.isdigit()]
    return " ".join(words[:6]).strip()


def _row_reason(c: Candidate) -> str:
    return {
        "exact": "Exact match",
        "technique": "Matches the technique",
        "related": "Related technique",
        "spelling": "Closest spelling",
        "name": "Name match",
        "category": "Same category",
        "description": "Mentioned in the description",
        "capability": "Can do this",
        "similar": "Similar instrument",
    }.get(c.reason, "")


def option_row(c: Candidate) -> dict[str, Any]:
    eq = c.eq
    dept = getattr(eq, "internal_department", None)
    return {
        "equipment_id": int(eq.pk),
        "name": eq.name,
        "code": eq.code or "",
        "department": getattr(dept, "name", "") or "",
        "location": " ".join(str(eq.location or "").split())[:120],
        "category": getattr(getattr(eq, "category", None), "name", "") or "",
        "reason": _row_reason(c),
        "bookable": (eq.status or "").strip() == "ACTIVE",
        "status_label": eq.get_status_display() if eq.status else "Unknown",
    }


def capability_search(user, text: str, *, limit: int = 8) -> tuple[list[Candidate], list[str]]:
    """Active visible equipment able to do what the text describes (technique or research purpose)."""
    techs = [t.key for t in terminology.find_techniques(text)] or [t.key for t in terminology.techniques_for_purpose(text)]
    if not techs:
        return [], []
    labels = [terminology.TECHNIQUES[k].label for k in techs if k in terminology.TECHNIQUES]
    words = [w for w in norm(text).split() if len(w) >= 4 and w not in _STOP and w not in {
        "which", "equipment", "instrument", "measure", "measuring", "analyse", "analyze", "analysis", "determine",
        "characterise", "characterize", "characterization", "characterisation", "study", "detect", "image", "imaging",
    }]
    out: list[Candidate] = []
    for f in visible_pool(user):
        if not f.active:
            continue
        score = 0.0
        if techs and set(techs) & f.techniques:
            score = 0.9
        elif techs and any(n in f.name_c or n in f.description.replace(" ", "") for k in techs for n in _NEEDLES.get(k, ())):
            score = 0.7
        if words:
            hits = sum(1 for w in words if w in f.description or w in f.category or w in f.tokens)
            if hits:
                score = max(score, min(0.4 + 0.15 * hits, 0.75))
        if score >= 0.55:
            out.append(Candidate(f.eq, score, "capability"))
    out.sort(key=lambda c: (-c.score, c.eq.name or ""))
    return _collapse_modes(out)[:limit], labels


def similar_equipment(user, eq, *, limit: int = 3, pool: list[EqFeatures] | None = None) -> list[Candidate]:
    """Active visible instruments sharing a technique, category or equipment group (excluding its mode family)."""
    pool = pool if pool is not None else visible_pool(user)
    base = features(eq)
    family = {int(eq.pk)}
    if getattr(eq, "parent_equipment_id", None):
        family.add(int(eq.parent_equipment_id))
    out: list[Candidate] = []
    for f in pool:
        pk = int(f.eq.pk)
        if pk in family or (f.parent_id and int(f.parent_id) in family) or not f.active:
            continue
        score = 0.0
        if base.techniques and base.techniques & f.techniques:
            score = 0.9
        elif "fesem" in base.techniques and "sem" in f.techniques or "sem" in base.techniques and "fesem" in f.techniques:
            score = 0.75
        group = getattr(eq, "equipment_group_id", None)
        if group and getattr(f.eq, "equipment_group_id", None) == group:
            score = max(score, 0.85)
        if base.category and f.category == base.category:
            score = max(score, 0.6)
        if score:
            out.append(Candidate(f.eq, score, "similar"))
    out.sort(key=lambda c: (-c.score, c.eq.name or ""))
    return out[:limit]
