"""Header search: typo-tolerant, technique-aware equipment matching; bookings and people stay exact."""

from __future__ import annotations

import time

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from iic_booking.equipment import search_index, testdata
from iic_booking.equipment.models import Equipment, EquipmentCategory, SearchSynonym
from iic_booking.equipment.search_synonyms_seed import SEED, seed_search_synonyms
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.equipment.testdata_models import TestDataKind
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def catalog():
    from iic_booking.users.models.user_group import UserGroup

    Equipment.objects.all().delete()
    SearchSynonym.objects.all().delete()
    f = _EgsFactory()
    xrd = EquipmentCategory.objects.create(name="X-Ray Diffraction", code="XRD")
    thermal = EquipmentCategory.objects.create(name="Thermal Analysis", code="TA")
    printers = EquipmentCategory.objects.create(name="3D Printers", code="P3D")
    em = EquipmentCategory.objects.create(name="Electron Microscopy", code="EM")
    spectro = EquipmentCategory.objects.create(name="Spectroscopy", code="SPEC")
    test_cat = EquipmentCategory.objects.create(name="Sandbox", code="SANDBOX")
    eq = {
        "pxrd": f.equipment(name="Powder X-Ray Diffractometer (PXRD) [A]", code="PXRD [A]", category=xrd),
        "gixrd": f.equipment(
            name="Grazing Incidence X-Ray Diffractometer (GI-XRD)", code="GI-XRD", category=xrd, status="REPAIR"
        ),
        "fesem": f.equipment(
            name="Field Emission Scanning Electron Microscope (FESEM)-APREO", code="APREO", category=em
        ),
        "tem": f.equipment(name="Transmission Electron Microscope (TEM)", code="TEM", category=em),
        "xps": f.equipment(name="X-Ray Photoelectron Spectroscopy (XPS)", code="XPS", category=spectro),
        "xrf": f.equipment(name="X-Ray Fluorescence (XRF)", code="XRF", category=spectro),
        "icp": f.equipment(
            name="Inductively Coupled Plasma Mass Spectrometry (ICPMS-MS)", code="ICPMS/MS", category=spectro
        ),
        "tga": f.equipment(
            name="Thermo Gravimetric/ Differential Thermal Analyzer (TGA/DTA) [A]", code="TGA/DTA [A]", category=thermal
        ),
        "printer": f.equipment(name="Polymer FDM 3D Printer-I", code="P1", category=printers),
        "laser": f.equipment(name="Desktop Laser Cutter", code="L1"),
        "lathe": f.equipment(name="CNC Lathe", code="CNCL1"),
        "private": f.equipment(name="Private X-Ray Diffractometer (PRXRD)", code="PRXRD", category=xrd),
        "marked": f.equipment(name="Training X-Ray Diffractometer (TXRD)", code="TXRD", category=xrd),
        "in_test_cat": f.equipment(name="Sandbox Diffractometer", code="SBX", category=test_cat),
    }
    group = UserGroup.objects.create(name="Private search", code="PRIVSEARCH")
    eq["private"].visibility_group = group
    eq["private"].save(update_fields=["visibility_group"])
    testdata.mark(TestDataKind.EQUIPMENT, eq["marked"], reason="test")
    testdata.mark(TestDataKind.CATEGORY, test_cat, reason="test")
    seed_search_synonyms(Equipment, EquipmentCategory, SearchSynonym)
    search_index.invalidate()
    return {"eq": eq, "f": f, "group": group}


def _search(q, user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    res = client.get("/api/search/", {"q": q})
    assert res.status_code == 200
    return res.json()


def _codes(data):
    return [e["code"] for e in data["equipment"]]


def test_seed_links_the_codes_and_categories_that_exist(catalog):
    assert SearchSynonym.objects.count() == len(SEED) >= 25
    xrd = SearchSynonym.objects.get(technique="X-ray diffraction")
    assert list(xrd.categories.values_list("code", flat=True)) == ["XRD"]
    sem = SearchSynonym.objects.get(technique="Scanning electron microscopy")
    assert list(sem.equipment.values_list("code", flat=True)) == ["APREO"]
    assert seed_search_synonyms(Equipment, EquipmentCategory, SearchSynonym) == 0


def test_exact_code_beats_prefix_beats_technique(catalog):
    data = _search("TEM")
    assert _codes(data)[0] == "TEM"
    assert data["equipment"][0]["match"] == "exact"
    assert data["equipment"][0]["hint"] == ""

    data = _search("xrd")
    codes = _codes(data)
    assert codes[0] == "GI-XRD"
    assert "PXRD [A]" in codes
    assert all(e["match"] in ("exact", "contains", "prefix") for e in data["equipment"])


@pytest.mark.parametrize(
    "query, expected, technique",
    [
        ("crystal structure", {"PXRD [A]", "GI-XRD"}, "X-ray diffraction"),
        ("surface morphology", {"APREO"}, "Scanning electron microscopy"),
        ("thermal stability", {"TGA/DTA [A]"}, "Thermal analysis"),
        ("3d print", {"P1"}, None),
        ("laser cut", {"L1"}, None),
        ("additive manufacturing", {"P1"}, "3D printing"),
        ("laser engraving", {"L1"}, "Laser cutting and engraving"),
        ("cnc", {"CNCL1"}, None),
        ("prototype", {"P1", "CNCL1"}, None),
    ],
)
def test_techniques_find_instruments(catalog, query, expected, technique):
    data = _search(query)
    codes = set(_codes(data))
    assert expected <= codes
    if technique:
        hinted = [e for e in data["equipment"] if e["code"] in expected]
        assert all(e["hint"] == f"Matched technique: {technique}" for e in hinted)


def test_elemental_composition_finds_eds_xrf_and_icp(catalog):
    codes = set(_codes(_search("elemental composition")))
    assert {"APREO", "TEM", "XRF", "ICPMS/MS"} <= codes
    assert "XPS" not in codes


def test_diffraction_ranks_literal_hits_before_techniques(catalog):
    data = _search("diffraction")
    kinds = [e["match"] for e in data["equipment"]]
    assert kinds == sorted(kinds, key=["exact", "prefix", "contains", "technique", "fuzzy"].index)
    assert {"PXRD [A]", "GI-XRD", "TEM"} <= set(_codes(data))


@pytest.mark.parametrize(
    "query, code, corrected",
    [("fesm", "APREO", "fesem"), ("xpr", "XPS", "xps"), ("xdr", "GI-XRD", "xrd"), ("difractometer", "PXRD [A]", None)],
)
def test_typos_still_match(catalog, query, code, corrected):
    data = _search(query)
    assert code in _codes(data)
    hit = next(e for e in data["equipment"] if e["code"] == code)
    assert hit["match"] == "fuzzy"
    if corrected:
        assert data["suggestion"] == corrected


def test_typo_in_technique_shows_technique_hint(catalog):
    data = _search("cristal structure")
    assert {"PXRD [A]", "GI-XRD"} <= set(_codes(data))
    assert data["equipment"][0]["hint"] == "Matched technique: X-ray diffraction"
    assert data["suggestion"] == "crystal structure"


def test_known_words_are_not_corrected(catalog):
    # "sem" is a real term: it must not be "corrected" to TEM.
    assert "TEM" not in _codes(_search("sem"))
    assert _search("qqqzz")["equipment"] == []


def test_visibility_and_test_data_exclusion(catalog):
    eq = catalog["eq"]
    anon = set(_codes(_search("diffractometer")))
    assert "PRXRD" not in anon
    assert "TXRD" not in anon and "SBX" not in anon

    member = catalog["f"].student()
    catalog["group"].members.create(user=member)
    assert "PRXRD" in _codes(_search("PRXRD", member))

    admin = UserFactory(user_type=UserType.ADMIN)
    admin_codes = set(_codes(_search("diffractometer", admin)))
    assert "TXRD" not in admin_codes and "SBX" not in admin_codes

    tester = catalog["f"].student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    assert "TXRD" in _codes(_search("TXRD", tester))
    assert eq["gixrd"].status == "REPAIR"


def test_non_operational_equipment_keeps_its_status(catalog):
    hit = next(e for e in _search("GI-XRD")["equipment"] if e["code"] == "GI-XRD")
    assert hit["status"] == "REPAIR"


def test_bookings_and_people_are_never_fuzzy(catalog):
    f, eq = catalog["f"], catalog["eq"]
    from iic_booking.users.models import User

    alice = f.student()
    User.objects.filter(pk=alice.pk).update(name="Zelinda Quorvath", is_active=True)
    booking = f.booking(alice, eq["xps"], f.future(days=2))
    admin = UserFactory(user_type=UserType.ADMIN)

    exact = _search("Zelinda", admin)
    assert [p["id"] for p in exact["people"]] == [alice.pk]
    near = _search("Zelinde", admin)
    assert near["people"] == [] and near["bookings"] == []

    assert [b["id"] for b in _search(booking.virtual_booking_id, admin)["bookings"]] == [booking.pk]
    assert _search(booking.virtual_booking_id[:-1] + "Q", admin)["bookings"] == []


def test_role_scoping_unchanged(catalog):
    f, eq = catalog["f"], catalog["eq"]
    owner, other = f.student(), f.student()
    mine = f.booking(owner, eq["xps"], f.future(days=2))
    theirs = f.booking(other, eq["xps"], f.future(days=3))

    data = _search("XPS", owner)
    ids = {b["id"] for b in data["bookings"]}
    assert mine.pk in ids and theirs.pk not in ids
    assert data["people_enabled"] is False and data["people"] == []

    anon = _search("XPS")
    assert anon["bookings"] == [] and anon["people"] == [] and "staff_view" not in anon


def test_index_is_cached_and_rebuilt_after_changes(catalog, django_assert_max_num_queries):
    _search("xrd")
    with django_assert_max_num_queries(8):
        _search("xrd")
    syn = SearchSynonym.objects.get(technique="Thermal analysis")
    syn.terms = syn.terms + ", heat flow"
    syn.save()
    assert "TGA/DTA [A]" in _codes(_search("heat flow"))
    syn.equipment.add(catalog["eq"]["tem"])
    assert "TEM" in _codes(_search("heat flow"))
    catalog["eq"]["lathe"].name = "CNC Turning Centre"
    catalog["eq"]["lathe"].save()
    assert "CNCL1" in _codes(_search("turning centre"))


def test_search_stays_fast(catalog):
    _search("warm up")
    started = time.perf_counter()
    for q in ("crystal structure", "fesm", "xrd", "surface morphology", "thermal stablity"):
        _search(q)
    assert (time.perf_counter() - started) / 5 < 0.15


def test_main_admin_edits_synonyms(catalog):
    admin = UserFactory(user_type=UserType.ADMIN)
    client = APIClient()
    client.force_authenticate(user=admin)
    listing = client.get("/api/admin/search-synonyms/").json()
    assert len(listing["synonyms"]) == len(SEED)
    assert "SBX" not in {e["code"] for e in listing["equipment"]}

    res = client.post(
        "/api/admin/search-synonyms/",
        {"technique": "Rheology", "terms": "rheometer, Viscosity,  viscosity", "equipment_ids": [catalog["eq"]["tem"].pk]},
        format="json",
    )
    assert res.status_code == 201
    assert res.json()["terms"] == ["rheometer", "viscosity"]
    assert "TEM" in _codes(_search("viscosity"))

    pk = res.json()["id"]
    assert client.patch(f"/api/admin/search-synonyms/{pk}/", {"is_active": False}, format="json").status_code == 200
    assert "TEM" not in _codes(_search("viscosity"))
    assert client.post("/api/admin/search-synonyms/", {"technique": "", "terms": ""}, format="json").status_code == 400
    assert client.delete(f"/api/admin/search-synonyms/{pk}/").status_code == 204

    student = catalog["f"].student()
    other = APIClient()
    other.force_authenticate(user=student)
    assert other.get("/api/admin/search-synonyms/").status_code == 403
