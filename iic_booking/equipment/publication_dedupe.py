"""Unique publications: ``EquipmentPublication`` rows grouped so a paper counts and lists once.

A row links one publication to one instrument, so the same paper appears once per instrument it used and sometimes
twice for one instrument (entered by hand and again from a claim). Rows are never changed; they are grouped:

1. Same DOI: from the DOI field, else found in the URL or the citation; ``https://doi.org/``, ``dx.doi.org``,
   ``doi:`` and case do not matter.
2. No DOI: the same normalised title (case, accents, punctuation and spacing ignored) and the same year. Titles that
   are only similar, or the same title in different years, stay separate.
3. A row without a DOI joins a DOI publication of the same title and year when exactly one such DOI publication
   exists (the same paper entered once with and once without its DOI); with two different DOIs it stays apart.

The grouping is cached and rebuilt when publications change. Count, search, filters, sorting and pages all work on the
groups, never on raw rows.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import unquote

from django.core.cache import cache

CACHE_KEY = "equipment:unique_publications:v1"
CACHE_SECONDS = 600

_DOI = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_TRAILING = ".,;:)]}"


def normalize_doi(*values: str | None) -> str:
    """The first DOI found in ``values``, lower-case, without resolver prefix or trailing punctuation ("" if none)."""
    for value in values:
        if not value:
            continue
        match = _DOI.search(unquote(str(value)))
        if match:
            return match.group(0).rstrip(_TRAILING).lower()
    return ""


def normalize_title(title: str | None) -> str:
    text = unicodedata.normalize("NFKD", str(title or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.findall(r"[a-z0-9]+", text))


@dataclass
class UniquePublication:
    key: str
    rows: list[dict] = field(default_factory=list)

    @property
    def lead(self) -> dict:
        """The row shown: the most complete one (DOI, link, citation), then the oldest."""
        return min(self.rows, key=lambda r: (not r["doi_n"], not r["url"], not r["citation"], r["id"]))

    def as_dict(self) -> dict:
        lead = self.lead
        href = (lead["url"] or "").strip()
        doi = lead["doi_n"] or next((r["doi_n"] for r in self.rows if r["doi_n"]), "")
        if not href and doi:
            href = f"https://doi.org/{doi}"
        instruments, seen = [], set()
        for r in sorted(self.rows, key=lambda r: (r["equipment_name"] or "").lower()):
            if r["equipment_id"] is not None and r["equipment_id"] not in seen:
                seen.add(r["equipment_id"])
                instruments.append({"id": r["equipment_id"], "name": r["equipment_name"], "code": r["equipment_code"]})
        year = lead["year"] or next((r["year"] for r in self.rows if r["year"]), None)
        first = instruments[0] if instruments else {"id": None, "name": "", "code": ""}
        return {
            "id": lead["id"],
            "title": lead["title"],
            "citation": lead["citation"] or "",
            "url": href,
            "doi": doi,
            "year": year,
            "equipment": instruments,
            # Kept for older pages: the first instrument.
            "equipment_id": first["id"],
            "equipment_name": first["name"],
            "equipment_code": first["code"],
            "record_count": len(self.rows),
        }


def group_rows(rows: list[dict]) -> list[UniquePublication]:
    """Group raw rows (``id, title, citation, url, doi, year, equipment_*``) into unique publications."""
    groups: dict[str, UniquePublication] = {}
    by_title_year: dict[str, set[str]] = {}
    pending: list[dict] = []
    for r in rows:
        r["doi_n"] = normalize_doi(r.get("doi"), r.get("url"), r.get("citation"))
        title = normalize_title(r.get("title"))
        r["title_key"] = f"{title}|{r.get('year') or ''}" if title else ""
        if r["doi_n"]:
            key = f"doi:{r['doi_n']}"
            groups.setdefault(key, UniquePublication(key)).rows.append(r)
            if r["title_key"]:
                by_title_year.setdefault(r["title_key"], set()).add(key)
        else:
            pending.append(r)
    for r in pending:
        dois = by_title_year.get(r["title_key"], set()) if r["title_key"] else set()
        if len(dois) == 1:
            key = next(iter(dois))
        elif r["title_key"]:
            key = f"title:{r['title_key']}"
        else:
            key = f"id:{r['id']}"
        groups.setdefault(key, UniquePublication(key)).rows.append(r)
    return list(groups.values())


def _load() -> list[dict]:
    from .models import EquipmentPublication

    return [
        {
            "id": r["equipment_publication_id"],
            "title": r["title"],
            "citation": r["citation"],
            "url": r["url"],
            "doi": r["doi"],
            "year": r["year"],
            "equipment_id": r["equipment_id"],
            "equipment_name": r["equipment__name"],
            "equipment_code": r["equipment__code"],
        }
        for r in EquipmentPublication.objects.values(
            "equipment_publication_id", "title", "citation", "url", "doi", "year",
            "equipment_id", "equipment__name", "equipment__code",
        )
    ]


def unique_publications() -> list[dict]:
    """Every unique publication (as dicts), cached until a publication changes."""
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return cached
    out = [g.as_dict() for g in group_rows(_load())]
    cache.set(CACHE_KEY, out, CACHE_SECONDS)
    return out


def unique_publication_count() -> int:
    return len(unique_publications())


def invalidate(*_args, **_kwargs) -> None:
    cache.delete(CACHE_KEY)


SORTS = {
    "newest": lambda p: (-(p["year"] or 0), p["title"].lower(), p["id"]),
    "oldest": lambda p: ((p["year"] or 9999), p["title"].lower(), p["id"]),
    "title": lambda p: (p["title"].lower(), -(p["year"] or 0), p["id"]),
}


def query(*, q: str = "", equipment_id: int | None = None, year: int | None = None, sort: str = "newest",
          page: int = 1, page_size: int = 50) -> dict:
    """One page of unique publications after search and filters; ``count`` is the filtered number of unique ones."""
    items = unique_publications()
    if equipment_id is not None:
        items = [p for p in items if any(e["id"] == equipment_id for e in p["equipment"])]
    if year is not None:
        items = [p for p in items if p["year"] == year]
    needle = " ".join(str(q or "").lower().split())
    if needle:
        def hit(p):
            text = " ".join([p["title"], p["citation"], p["doi"], *[e["name"] or "" for e in p["equipment"]]]).lower()
            return all(word in text for word in needle.split())

        items = [p for p in items if hit(p)]
    items = sorted(items, key=SORTS.get(sort, SORTS["newest"]))
    page_size = max(1, min(int(page_size or 50), 100))
    total = len(items)
    num_pages = max((total + page_size - 1) // page_size, 1)
    page = max(1, min(int(page or 1), num_pages))
    start = (page - 1) * page_size
    return {
        "count": total,
        "page": page,
        "page_size": page_size,
        "num_pages": num_pages,
        "results": items[start : start + page_size],
    }


def facets() -> dict:
    """Instruments and years that have unique publications, for the list's filters."""
    instruments: dict[int, dict] = {}
    years: set[int] = set()
    for p in unique_publications():
        for e in p["equipment"]:
            row = instruments.setdefault(e["id"], {**e, "count": 0})
            row["count"] += 1
        if p["year"]:
            years.add(p["year"])
    return {
        "equipment": sorted(instruments.values(), key=lambda e: (e["name"] or "").lower()),
        "years": sorted(years, reverse=True),
    }


def connect_signals() -> None:
    from django.db.models.signals import post_delete, post_save

    from .models import Equipment, EquipmentPublication

    post_save.connect(invalidate, sender=EquipmentPublication, weak=False, dispatch_uid="unique_publications_save")
    post_delete.connect(invalidate, sender=EquipmentPublication, weak=False, dispatch_uid="unique_publications_delete")
    # Instrument names are shown with each publication.
    post_save.connect(invalidate, sender=Equipment, weak=False, dispatch_uid="unique_publications_equipment")
