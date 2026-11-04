"""Unique publications: the hero count and the publications list count each paper once."""

from __future__ import annotations

import uuid

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentPublication
from iic_booking.equipment.publication_dedupe import group_rows, normalize_doi, normalize_title

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _eq(name):
    return Equipment.objects.create(name=name, code=f"PB{uuid.uuid4().hex[:5].upper()}", slot_duration_minutes=60,
                                    user_rating_enabled=False)


def _pub(eq, title, **kw):
    return EquipmentPublication.objects.create(equipment=eq, title=title, **kw)


def _stats():
    return APIClient().get("/api/cms/site-stats/").json()["publication_count"]


def _list(**params):
    return APIClient().get("/api/cms/publications/", params).json()


def test_doi_normalisation_ignores_resolver_prefixes_and_case():
    assert normalize_doi("https://doi.org/10.1016/J.ACTAMAT.2024.001") == "10.1016/j.actamat.2024.001"
    assert normalize_doi("http://dx.doi.org/10.1016/j.actamat.2024.001.") == "10.1016/j.actamat.2024.001"
    assert normalize_doi("doi:10.1016/j.actamat.2024.001") == "10.1016/j.actamat.2024.001"
    assert normalize_doi("", "https://www.sciencedirect.com/x?doi=10.1016%2Fj.actamat.2024.001") == "10.1016/j.actamat.2024.001"
    assert normalize_doi("no doi here") == ""
    assert normalize_title("  Grain-Boundary  Effects in Ni–Ti Alloys. ") == "grain boundary effects in ni ti alloys"


def test_exact_duplicates_count_once():
    xrd = _eq("XRD")
    _pub(xrd, "Phase analysis of Ti alloys", year=2024)
    _pub(xrd, "Phase analysis of Ti alloys", year=2024)
    assert _stats() == 1
    body = _list()
    assert body["count"] == 1 and len(body["results"]) == 1 and body["results"][0]["record_count"] == 2


def test_same_doi_in_different_formats_and_instruments_is_one_publication():
    xrd, sem = _eq("XRD"), _eq("FESEM")
    _pub(xrd, "Microstructure of HEA", doi="10.1016/j.matlet.2025.100")
    _pub(sem, "Microstructure of high-entropy alloys", url="https://doi.org/10.1016/J.MATLET.2025.100")
    _pub(sem, "HEA microstructure", citation="Kumar A. Mater. Lett. (2025) doi:10.1016/j.matlet.2025.100")
    assert _stats() == 1
    (row,) = _list()["results"]
    assert row["doi"] == "10.1016/j.matlet.2025.100"
    assert sorted(e["name"] for e in row["equipment"]) == ["FESEM", "XRD"]


def test_title_and_year_match_without_doi():
    a, b = _eq("Raman"), _eq("FTIR")
    _pub(a, "Spectroscopic study of graphene oxide", year=2023)
    _pub(b, "Spectroscopic Study of Graphene-Oxide.", year=2023)
    assert _stats() == 1


def test_similar_but_distinct_publications_stay_apart():
    a = _eq("Raman")
    _pub(a, "Spectroscopic study of graphene oxide", year=2023)
    _pub(a, "Spectroscopic study of graphene oxide", year=2024)  # different year
    _pub(a, "Spectroscopic study of graphene oxide films", year=2023)  # different title
    _pub(a, "Same title, two DOIs", year=2022, doi="10.1000/aaa")
    _pub(a, "Same title, two DOIs", year=2022, doi="10.1000/bbb")
    assert _stats() == 5


def test_a_row_without_doi_joins_the_single_doi_publication_of_the_same_title():
    rows = [
        {"id": 1, "title": "Ferrite grain growth", "citation": "", "url": "", "doi": "10.5555/x", "year": 2021,
         "equipment_id": 1, "equipment_name": "A", "equipment_code": "A"},
        {"id": 2, "title": "Ferrite grain growth", "citation": "", "url": "", "doi": "", "year": 2021,
         "equipment_id": 2, "equipment_name": "B", "equipment_code": "B"},
    ]
    assert len(group_rows([dict(r) for r in rows])) == 1
    ambiguous = rows + [{**rows[0], "id": 3, "doi": "10.5555/y"}]
    assert len(group_rows([dict(r) for r in ambiguous])) == 3


def test_count_list_pagination_search_and_filters_use_unique_publications():
    xrd, sem = _eq("XRD"), _eq("FESEM")
    for i in range(7):
        _pub(xrd, f"Paper {i}", year=2020 + (i % 2), doi=f"10.1000/p{i}")
        _pub(sem, f"Paper {i}", year=2020 + (i % 2), url=f"https://doi.org/10.1000/P{i}")  # same paper, other instrument
    _pub(sem, "Only on FESEM", year=2019)
    assert _stats() == 8

    first = _list(page_size=3, page=1)
    assert first["count"] == 8 and first["num_pages"] == 3 and len(first["results"]) == 3
    last = _list(page_size=3, page=3)
    assert len(last["results"]) == 2
    seen = [r["id"] for p in (1, 2, 3) for r in _list(page_size=3, page=p)["results"]]
    assert len(seen) == len(set(seen)) == 8

    assert _list(equipment_id=xrd.equipment_id)["count"] == 7
    assert _list(year=2019)["count"] == 1
    assert _list(q="only fesem")["count"] == 1
    assert [r["title"] for r in _list(sort="title", page_size=2)["results"]] == ["Only on FESEM", "Paper 0"]
    facets = _list()["facets"]
    assert {e["name"]: e["count"] for e in facets["equipment"]} == {"FESEM": 8, "XRD": 7}
    assert facets["years"] == [2021, 2020, 2019]


def test_records_are_never_changed_and_new_ones_update_the_count():
    xrd = _eq("XRD")
    _pub(xrd, "A", doi="10.2000/a")
    assert _stats() == 1
    _pub(xrd, "B", doi="10.2000/b")
    assert _stats() == 2  # cache refreshed on save
    assert EquipmentPublication.objects.count() == 2
