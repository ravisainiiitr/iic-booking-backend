"""Typo-tolerant, technique-aware equipment search for the header search.

The catalogue is small, so every instrument's searchable text (name, code, abbreviations in brackets, category,
make, location, mode names and linked search synonyms) is kept in one cached index and scored in Python; no
database extension is needed. The index is rebuilt after equipment, categories or synonyms change.

Ranking, best first: exact code / name, prefix, contained text, technique (search synonym), then spelling-tolerant
matches. Technique and spelling matches carry a hint for the user.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from django.core.cache import cache
from django.db import DatabaseError, transaction

logger = logging.getLogger(__name__)

CACHE_KEY = "equipment:search_index:v1"
CACHE_SECONDS = 600

EXACT = 100
ABBREVIATION = 95
PREFIX = 90
WORD_PREFIX = 85
CONTAINS = 80
OTHER_FIELD = 75
ALL_WORDS = 72
TECHNIQUE_EXACT = 65
TECHNIQUE_PREFIX = 62
TECHNIQUE_IN_QUERY = 60
TECHNIQUE_WORDS = 58
FUZZY = 45
FUZZY_TECHNIQUE = 40

_TOKEN = re.compile(r"[a-z0-9]+")
_BRACKETS = re.compile(r"\(([^)]*)\)")
_STOP_WORDS = frozenset(
    "a an and by for from in into of on or the to with using via my sample samples test testing".split()
)

_memo: dict[str, Any] = {"stamp": None, "candidates": {}}


def tokens(text: Any) -> list[str]:
    return _TOKEN.findall(str(text or "").lower())


def norm(text: Any) -> str:
    return " ".join(tokens(text))


def compact(text: Any) -> str:
    return "".join(tokens(text))


def allowed_typos(word: str) -> int:
    n = len(word)
    if n < 3:
        return 0
    return 1 if n < 8 else 2


def distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance (insert, delete, substitute, swap neighbours); ``limit + 1`` when larger."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        row_min = i
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
            if v < row_min:
                row_min = v
        if row_min > limit:
            return limit + 1
        prev2, prev = prev, cur
    return prev[-1]


# --------------------------------------------------------------------------- index


def _synonym_rows() -> list[tuple[str, list[str], list[int], list[int]]] | None:
    """``(technique, terms, category ids, equipment ids)`` of active synonyms; ``None`` before the table exists."""
    from .search_synonym_models import SearchSynonym

    rows = []
    try:
        with transaction.atomic():
            for syn in SearchSynonym.objects.filter(is_active=True).prefetch_related("categories", "equipment"):
                rows.append(
                    (
                        syn.technique,
                        syn.term_list(),
                        [c.pk for c in syn.categories.all()],
                        [e.pk for e in syn.equipment.all()],
                    )
                )
    except DatabaseError:
        logger.warning("Search synonyms unavailable (table not migrated yet); searching without them")
        return None
    return rows


def build_index() -> dict[str, Any]:
    from .models import Equipment

    rows = list(
        Equipment.objects.values(
            "equipment_id", "name", "code", "make", "location", "parent_equipment_id", "category_id", "category__name"
        )
    )
    modes: dict[int, list[str]] = {}
    for r in rows:
        if r["parent_equipment_id"]:
            modes.setdefault(r["parent_equipment_id"], []).append(r["name"] or "")

    by_category: dict[int, list[tuple[str, str]]] = {}
    by_equipment: dict[int, list[tuple[str, str]]] = {}
    synonym_rows = _synonym_rows()
    for technique, terms, category_ids, equipment_ids in synonym_rows or []:
        pairs = [(norm(t), technique) for t in terms if norm(t)]
        for cid in category_ids:
            by_category.setdefault(cid, []).extend(pairs)
        for eid in equipment_ids:
            by_equipment.setdefault(eid, []).extend(pairs)

    entries = []
    vocab: set[str] = set()
    for r in rows:
        eid = r["equipment_id"]
        name, code = r["name"] or "", r["code"] or ""
        abbrevs = {compact(part) for part in _BRACKETS.findall(name)}
        abbrevs |= {t for part in _BRACKETS.findall(name) for t in tokens(part)}
        abbrevs |= {t for t in tokens(code) if len(t) >= 2}
        abbrevs.discard("")
        other = " ".join([r["category__name"] or "", r["make"] or "", r["location"] or "", *modes.get(eid, [])])
        own_words = {t for t in tokens(f"{name} {code} {other}") if len(t) >= 2} | abbrevs | {compact(code)}
        own_words.discard("")
        seen: set[str] = set()
        synonyms = []
        for term, technique in by_equipment.get(eid, []) + by_category.get(r["category_id"], []):
            if term not in seen:
                seen.add(term)
                synonyms.append((term, term.replace(" ", ""), technique))
        term_words: dict[str, str] = {}
        for term, _, technique in synonyms:
            for w in term.split():
                if len(w) >= 2:
                    term_words.setdefault(w, technique)
        vocab |= own_words | set(term_words)
        entries.append(
            {
                "id": eid,
                "name": name,
                "name_n": norm(name),
                "name_c": compact(name),
                "name_words": tokens(f"{name} {code}"),
                "code_c": compact(code),
                "abbrevs": abbrevs,
                "other_n": norm(other),
                "own_words": own_words,
                "synonyms": synonyms,
                "term_words": term_words,
            }
        )
    return {
        "stamp": time.time(),
        "entries": entries,
        "vocab": frozenset(vocab),
        "complete": synonym_rows is not None,
    }


def get_index() -> dict[str, Any]:
    index = cache.get(CACHE_KEY)
    if index is None:
        index = build_index()
        cache.set(CACHE_KEY, index, CACHE_SECONDS if index["complete"] else 60)
    return index


def invalidate(*_args, **_kwargs) -> None:
    cache.delete(CACHE_KEY)


# --------------------------------------------------------------------------- scoring


def _word_candidates(word: str, index: dict[str, Any]) -> tuple[str, dict[str, float]]:
    """How ``word`` occurs in the catalogue: ``("exact"|"prefix"|"fuzzy"|"none", {token: distance})``.

    Spelling tolerance only applies to words that are not in the catalogue at all, so a real code such as "sem" is
    never "corrected" to a neighbour such as "tem".
    """
    memo = _memo
    if memo["stamp"] != index["stamp"]:
        memo["stamp"], memo["candidates"] = index["stamp"], {}
    hit = memo["candidates"].get(word)
    if hit is not None:
        return hit
    vocab = index["vocab"]
    if word in vocab:
        hit = ("exact", {word: 0})
    else:
        prefixed = {t: 0 for t in vocab if len(word) >= 2 and t.startswith(word)}
        if prefixed:
            hit = ("prefix", prefixed)
        else:
            limit = allowed_typos(word)
            close = {}
            if limit:
                for t in vocab:
                    d = distance(word, t, limit)
                    if d <= limit:
                        # A wrong first letter is a less likely slip than one later in the word.
                        close[t] = d + (0.5 if t[0] != word[0] else 0)
            hit = ("fuzzy", close) if close else ("none", {})
    if len(memo["candidates"]) > 2000:
        memo["candidates"] = {}
    memo["candidates"][word] = hit
    return hit


def _query_words(qn: str) -> list[str]:
    words = qn.split()
    kept = [w for w in words if w not in _STOP_WORDS and len(w) >= 2]
    return kept or words


def score_entry(entry: dict[str, Any], qn: str, qc: str, words: list[str], index: dict[str, Any]):
    """``(score, kind, technique, corrections)`` for one instrument, or ``None`` when it does not match."""
    if qc == entry["code_c"] or qn == entry["name_n"] or qc == entry["name_c"]:
        return EXACT, "exact", "", {}
    if qc in entry["abbrevs"]:
        return ABBREVIATION, "exact", "", {}
    if entry["code_c"].startswith(qc) or entry["name_n"].startswith(qn):
        return PREFIX, "prefix", "", {}
    if all(any(w.startswith(q) for w in entry["name_words"]) for q in words):
        return WORD_PREFIX, "prefix", "", {}
    if qn in entry["name_n"] or qc in entry["code_c"] or (len(qc) >= 3 and qc in entry["name_c"]):
        return CONTAINS, "contains", "", {}
    if qn in entry["other_n"]:
        return OTHER_FIELD, "contains", "", {}

    best = None
    padded = f" {qn} "
    for term, term_c, technique in entry["synonyms"]:
        if qn == term or qc == term_c:
            s = TECHNIQUE_EXACT
        elif len(qn) >= 3 and term.startswith(qn):
            s = TECHNIQUE_PREFIX
        elif len(term) >= 2 and f" {term} " in padded:
            s = TECHNIQUE_IN_QUERY
        else:
            continue
        if best is None or s > best[0]:
            best = (s, "technique", technique, {})
    if best:
        return best

    own, term_words = entry["own_words"], entry["term_words"]
    used_terms: list[str] = []
    corrections: dict[str, str] = {}
    typos = 0
    for w in words:
        how, cands = _word_candidates(w, index)
        if how == "none":
            return None
        if how in ("exact", "prefix"):
            if w in own or any(t.startswith(w) for t in own):
                continue
            hits = sorted(t for t in term_words if t == w or (len(w) >= 3 and t.startswith(w)))
            if not hits:
                return None
            used_terms.append(term_words[hits[0]])
            continue
        match = min((t for t in cands if t in own or t in term_words), key=lambda t: (cands[t], t), default=None)
        if match is None:
            return None
        corrections[w] = match
        typos += cands[match]
        if match not in own:
            used_terms.append(term_words[match])

    technique = max(sorted(set(used_terms)), key=used_terms.count) if used_terms else ""
    if not corrections:
        return (TECHNIQUE_WORDS, "technique", technique, {}) if technique else (ALL_WORDS, "contains", "", {})
    return (FUZZY_TECHNIQUE if technique else FUZZY) - typos, "fuzzy", technique, corrections


def rank(q: str, allowed_ids: set[int], limit: int) -> tuple[list[dict[str, Any]], str]:
    """Best ``limit`` matches among ``allowed_ids``: ``[{id, kind, hint}]`` and a "did you mean" query or ``""``."""
    qn, qc = norm(q), compact(q)
    if not qc:
        return [], ""
    words = _query_words(qn)
    index = get_index()
    scored = []
    for entry in index["entries"]:
        if entry["id"] not in allowed_ids:
            continue
        hit = score_entry(entry, qn, qc, words, index)
        if hit:
            scored.append((hit, entry))
    scored.sort(key=lambda x: (-x[0][0], x[1]["name"].lower(), x[1]["id"]))

    suggestion = ""
    out = []
    for (score, kind, technique, corrections), entry in scored[:limit]:
        corrected = " ".join(corrections.get(w, w) for w in qn.split()) if corrections else ""
        if technique:
            hint = f"Matched technique: {technique}"
        elif kind == "fuzzy" and corrected:
            hint = f"Did you mean “{corrected}”?"
        else:
            hint = ""
        if kind == "fuzzy" and corrected and not suggestion and not out:
            suggestion = corrected
        out.append({"id": entry["id"], "kind": kind, "hint": hint, "score": score})
    return out, suggestion


# --------------------------------------------------------------------------- invalidation


def connect_signals() -> None:
    from django.db.models.signals import m2m_changed, post_delete, post_save

    from .models import Equipment, EquipmentCategory
    from .search_synonym_models import SearchSynonym

    for model in (Equipment, EquipmentCategory, SearchSynonym):
        post_save.connect(invalidate, sender=model, dispatch_uid=f"search_index_save_{model.__name__}")
        post_delete.connect(invalidate, sender=model, dispatch_uid=f"search_index_delete_{model.__name__}")
    for through in (SearchSynonym.categories.through, SearchSynonym.equipment.through):
        m2m_changed.connect(invalidate, sender=through, dispatch_uid=f"search_index_m2m_{through.__name__}")
