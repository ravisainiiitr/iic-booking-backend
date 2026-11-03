"""Department-wise I-STEM mapping: per-department progress, I-STEM's own listing, name suggestions and saving several
mappings at once. Main Admin chooses any department; a department administrator only their own. Fake transport."""

from __future__ import annotations

import pytest
from django.core.cache import cache

from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.equipment_mapping import (
    LISTING_CACHE_KEY, LISTING_SNAPSHOT_KEY, MappingRefused, istem_listing, match_score, save_many, suggestions,
)
from iic_booking.istem.models import IstemEquipmentMapping

from .support import FakeTransport, apply_live, token_response
from .test_equipment_mapping_page import URL, _admin, _api, _department, _dept_admin, _equipment, _page

pytestmark = pytest.mark.django_db

LISTING = [
    {"equipment_id": 38750, "equipment_name": "X-ray Fluorescence (XRF) -IIC", "institute_id": 213},
    {"equipment_id": 38751, "equipment_name": "Field Emission Scanning Electron Microscope (FESEM)", "institute_id": 213},
    {"equipment_id": 38752, "equipment_name": "Powder X-ray Diffractometer (XRD)", "institute_id": 213},
    {"equipment_id": 38753, "equipment_name": "Liquid Nitrogen Plant", "institute_id": 213},
]


@pytest.fixture(autouse=True)
def _fresh():
    clear_token_cache()
    cache.delete(LISTING_CACHE_KEY)
    cache.delete(LISTING_SNAPSHOT_KEY)
    yield
    clear_token_cache()
    cache.delete(LISTING_CACHE_KEY)
    cache.delete(LISTING_SNAPSHOT_KEY)


def _fake(rows=LISTING):
    transport = FakeTransport(token_response(), _page(rows))
    return transport, (lambda: IstemClient(transport))


@pytest.mark.parametrize("portal, code, istem, expected", [
    ("XRF", "XRF-01", "X-ray Fluorescence (XRF) -IIC", True),
    ("Field Emission SEM", "FESEM-01", "Field Emission Scanning Electron Microscope (FESEM)", True),
    ("X-Ray Diffractometer", "XRD-B1", "Powder X-ray Diffractometer (XRD)", True),
    ("Field Emission SEM", "FESEM-01", "Liquid Nitrogen Plant", False),
])
def test_match_score(portal, code, istem, expected):
    assert (match_score(portal, code, istem) >= 0.45) is expected


def test_list_shows_progress_per_department():
    chem, phys = _department("C"), _department("P")
    xrf = _equipment(chem, "XRF", fbr=True)
    _equipment(chem, "XRD", fbr=True)
    _equipment(phys, "FESEM")
    IstemEquipmentMapping.objects.create(equipment=xrf, istem_equipment_id="38750", sample_count_input_key="A")
    body = _api(_admin()).get(URL).data
    by_name = {d["name"]: d for d in body["departments"]}
    assert by_name[chem.name] == {"id": chem.pk, "name": chem.name, "total": 2, "mapped": 1, "fbr_unmapped": 1,
                                  "istem_department_ids": []}
    assert by_name[phys.name]["total"] == 1 and by_name[phys.name]["mapped"] == 0


def test_istem_listing_shows_what_each_istem_equipment_is_mapped_to(settings):
    apply_live(settings)
    xrf = _equipment(_department("C"), "XRF")
    IstemEquipmentMapping.objects.create(equipment=xrf, istem_equipment_id="38750")
    transport, factory = _fake()
    out = istem_listing(_admin(), client_factory=factory)
    rows = {r["equipment_id"]: r for r in out["equipment"]}
    assert out["total"] == 4 and out["mapped"] == 1
    assert rows["38750"]["mapped_to"] == [{"equipment_id": xrf.pk, "name": "XRF"}] and rows["38753"]["mapped_to"] == []
    # Cached: a second look does not call I-STEM again.
    istem_listing(_admin(), client_factory=factory)
    assert len([c for c in transport.calls if "equipment-list" in c["url"]]) == 1


def test_listing_needs_istem_connected():
    with pytest.raises(MappingRefused) as offline:
        istem_listing(_admin())
    assert offline.value.http_status == 503


def test_suggestions_for_a_department_match_by_name_and_skip_mapped(settings):
    apply_live(settings)
    chem, phys = _department("C"), _department("P")
    xrf = _equipment(chem, "XRF", fbr=True)
    fesem = _equipment(chem, "Field Emission SEM", keys=("A", "B"))
    _equipment(phys, "X-Ray Diffractometer")
    done = _equipment(chem, "Tabletop SEM")
    IstemEquipmentMapping.objects.create(equipment=done, istem_equipment_id="38753")
    _, factory = _fake()
    out = suggestions(_admin(), department_id=chem.pk, client_factory=factory)
    rows = {r["name"]: r for r in out["equipment"]}
    assert set(rows) == {"XRF", "Field Emission SEM"}
    assert rows["XRF"]["candidates"][0]["equipment_id"] == "38750" and rows["XRF"]["requires_istem_fbr"]
    assert rows["Field Emission SEM"]["candidates"][0]["equipment_id"] == "38751"
    assert rows["XRF"]["default_sample_key"] == "A"
    assert all(c["score"] >= 0.45 for r in rows.values() for c in r["candidates"])
    assert xrf.pk in {r["equipment_id"] for r in out["equipment"]} and fesem.pk in rows["Field Emission SEM"].values()


def test_a_department_administrator_only_gets_their_department(settings):
    apply_live(settings)
    chem, phys = _department("C"), _department("P")
    _equipment(chem, "XRF")
    _equipment(phys, "Field Emission SEM")
    _, factory = _fake()
    out = suggestions(_dept_admin(chem), department_id=phys.pk, client_factory=factory)
    assert out["department_id"] == chem.pk and [r["name"] for r in out["equipment"]] == ["XRF"]


def test_users_without_equipment_rights_are_refused(settings):
    apply_live(settings)
    chem = _department("C")
    res = _api(_dept_admin(chem, grant=False)).get(f"{URL}suggestions/", {"department_id": chem.pk})
    assert res.status_code == 403
    assert _api(_dept_admin(chem, grant=False)).post(f"{URL}bulk/", {"mappings": []}, format="json").status_code in (400, 403)


def test_save_many_saves_each_row_on_its_own(settings):
    chem, phys = _department("C"), _department("P")
    xrf = _equipment(chem, "XRF")
    xrd = _equipment(chem, "XRD")
    other = _equipment(phys, "FESEM")
    out = save_many(_dept_admin(chem), [
        {"equipment_id": xrf.pk, "istem_equipment_id": "38750", "sample_count_input_key": "A"},
        {"equipment_id": xrd.pk, "istem_equipment_id": "not-a-number"},
        {"equipment_id": other.pk, "istem_equipment_id": "38751", "sample_count_input_key": "A"},
    ])
    assert out["saved"] == 1 and out["failed"] == 2 and out["checked"] is False
    assert [r["ok"] for r in out["results"]] == [True, False, False]
    assert IstemEquipmentMapping.objects.get(equipment=xrf).istem_equipment_id == "38750"
    assert not IstemEquipmentMapping.objects.filter(equipment__in=[xrd, other]).exists()


def test_bulk_endpoint_checks_the_saved_ids_on_istem_once(settings):
    apply_live(settings)
    chem = _department("C")
    xrf = _equipment(chem, "XRF")
    fesem = _equipment(chem, "Field Emission SEM")
    from unittest.mock import patch

    transport, factory = _fake()
    with patch("iic_booking.istem.service.default_client", factory):
        res = _api(_admin()).post(f"{URL}bulk/", {"mappings": [
            {"equipment_id": xrf.pk, "istem_equipment_id": "38750", "sample_count_input_key": "A"},
            {"equipment_id": fesem.pk, "istem_equipment_id": "38751", "sample_count_input_key": "A"},
        ]}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["saved"] == 2 and res.data["checked"] is True
    assert IstemEquipmentMapping.objects.get(equipment=fesem).remote_found is True
    assert len([c for c in transport.calls if "equipment-list" in c["url"]]) == 1


def test_bulk_needs_rows():
    res = _api(_admin()).post(f"{URL}bulk/", {"mappings": []}, format="json")
    assert res.status_code == 400


# --- I-STEM departments and automatic, department-narrowed matching ---------------------------------------------------

DEPT_LISTING = [
    {"equipment_id": 38750, "equipment_name": "X-ray Fluorescence (XRF) -IIC", "institute_id": 213, "equipment_abbr": "XRF",
     "equipment_department_id": "164", "equipment_location": "Analytical Section -1, IIC", "equipment_model": "S8 Tiger",
     "equipment_make": "Bruker", "equipment_srno": "BX-1001"},
    {"equipment_id": 38760, "equipment_name": "X-ray Fluorescence portable", "institute_id": 213, "equipment_abbr": "XRF",
     "equipment_department_id": "171", "equipment_location": "Earth Sciences", "equipment_model": "Vanta",
     "equipment_make": "Olympus", "equipment_srno": "OL-77"},
    {"equipment_id": 38751, "equipment_name": "Field Emission Scanning Electron Microscope (FESEM)", "institute_id": 213,
     "equipment_abbr": "FESEM", "equipment_department_id": "164", "equipment_location": "Analytical Section -2, IIC",
     "equipment_model": "Gemini 300", "equipment_make": "Zeiss", "equipment_srno": "ZS-90017"},
    {"equipment_id": 38770, "equipment_name": "Rock Cutting Machine", "institute_id": 213,
     "equipment_department_id": "0"},
]


def test_listing_keeps_department_short_name_make_model_serial_and_location(settings):
    apply_live(settings)
    _, factory = _fake(DEPT_LISTING)
    rows = {r["equipment_id"]: r for r in istem_listing(_admin(), client_factory=factory)["equipment"]}
    assert rows["38750"]["department_id"] == "164" and rows["38750"]["abbr"] == "XRF"
    assert rows["38750"]["serial"] == "BX-1001" and rows["38750"]["location"].startswith("Analytical")
    assert rows["38770"]["department_id"] == ""


def test_istem_departments_are_listed_with_a_suggestion_from_mapped_instruments(settings):
    apply_live(settings)
    iic = _department("I")
    fesem = _equipment(iic, "FESEM")
    IstemEquipmentMapping.objects.create(equipment=fesem, istem_equipment_id="38751", sample_count_input_key="A")
    _, factory = _fake(DEPT_LISTING)
    from iic_booking.istem.equipment_mapping import istem_departments

    out = istem_departments(_admin(), client_factory=factory)
    rows = {r["istem_department_id"]: r for r in out["istem_departments"]}
    assert rows["164"]["equipment_count"] == 2 and rows["164"]["suggested"] == {"id": iic.pk, "name": iic.name, "instruments": 1}
    assert rows["164"]["locations"] and rows["164"]["department"] is None
    assert rows["171"]["suggested"] is None and rows[""]["equipment_count"] == 1
    with pytest.raises(MappingRefused):
        istem_departments(_dept_admin(iic), client_factory=factory)


def test_department_mappings_are_main_admin_only_and_saved(settings):
    from iic_booking.istem.models import IstemDepartmentMapping

    iic = _department("I")
    url = "/api/istem/department-mappings/"
    assert _api(_dept_admin(iic)).put(url, {"mappings": [{"istem_department_id": "164", "department_id": iic.pk}]},
                                      format="json").status_code == 403
    res = _api(_admin()).put(url, {"mappings": [{"istem_department_id": "164", "department_id": iic.pk},
                                                {"istem_department_id": "165", "department_id": iic.pk}]}, format="json")
    assert res.status_code == 200 and res.data["saved"] == 2
    assert set(IstemDepartmentMapping.objects.filter(department=iic).values_list("istem_department_id", flat=True)) == {"164", "165"}
    res = _api(_admin()).put(url, {"mappings": [{"istem_department_id": "165", "department_id": None}]}, format="json")
    assert res.data["removed"] == 1


def test_suggestions_search_only_the_departments_istem_departments_and_flag_confident_matches(settings):
    from iic_booking.istem.models import IstemDepartmentMapping

    apply_live(settings)
    iic = _department("I")
    xrf = _equipment(iic, "XRF Spectrometer", fbr=True)
    xrf.model_information, xrf.make = "S8 TIGER", "Bruker AXS"
    xrf.save()
    fesem = _equipment(iic, "Field Emission SEM")
    fesem.asset_serial_number = "zs 90017"
    fesem.save()
    IstemDepartmentMapping.objects.create(istem_department_id="164", department=iic)
    _, factory = _fake(DEPT_LISTING)
    out = suggestions(_admin(), department_id=iic.pk, client_factory=factory)
    rows = {r["name"]: r for r in out["equipment"]}
    assert rows["XRF Spectrometer"]["istem_departments"] == ["164"]
    ids = [c["equipment_id"] for c in rows["XRF Spectrometer"]["candidates"]]
    assert ids[0] == "38750" and "38760" not in ids  # the portable XRF of another I-STEM department is not offered
    assert rows["XRF Spectrometer"]["candidates"][0]["reasons"][0] == "model" and rows["XRF Spectrometer"]["confident"]
    assert rows["Field Emission SEM"]["candidates"][0]["reasons"][0] == "serial number"
    assert rows["Field Emission SEM"]["candidates"][0]["score"] == 1.0 and rows["Field Emission SEM"]["confident"]


def test_without_a_department_mapping_the_whole_list_is_searched(settings):
    apply_live(settings)
    xrf = _equipment(_department("C"), "XRF")
    _, factory = _fake(DEPT_LISTING)
    row = suggestions(_admin(), equipment_id=xrf.pk, client_factory=factory)["equipment"][0]
    assert row["istem_departments"] == []
    assert {"38750", "38760"} <= {c["equipment_id"] for c in row["candidates"]}
    assert row["confident"] is False  # two XRFs close together: the admin picks


def test_one_instrument_can_be_suggested_even_when_mapped(settings):
    apply_live(settings)
    chem = _department("C")
    xrf = _equipment(chem, "XRF")
    IstemEquipmentMapping.objects.create(equipment=xrf, istem_equipment_id="38750")
    _, factory = _fake(DEPT_LISTING)
    row = suggestions(_admin(), equipment_id=xrf.pk, client_factory=factory)["equipment"][0]
    assert row["candidates"][0]["equipment_id"] == "38750" and row["candidates"][0]["mapped_to"] == []
    with pytest.raises(MappingRefused):
        suggestions(_dept_admin(_department("P")), equipment_id=xrf.pk, client_factory=factory)


# --- While I-STEM is not answering ------------------------------------------------------------------------------------

def _down():
    from iic_booking.istem.choices import ErrorCategory
    from iic_booking.istem.errors import IstemError

    transport = FakeTransport(IstemError(ErrorCategory.CONNECT_FAILED, "connect timeout"))
    return lambda: IstemClient(transport)


def test_the_last_list_read_is_used_while_istem_is_down(settings):
    apply_live(settings)
    _, factory = _fake(DEPT_LISTING)
    first = istem_listing(_admin(), client_factory=factory)
    assert first["istem_source"]["live"] is True
    out = istem_listing(_admin(), refresh=True, client_factory=_down())
    assert out["total"] == len(DEPT_LISTING)
    assert out["istem_source"]["live"] is False and out["istem_source"]["read_at"] == first["istem_source"]["read_at"]
    assert "not answering" in out["istem_source"]["problem"]


def test_without_an_earlier_read_the_admin_is_told_istem_is_down(settings):
    apply_live(settings)
    with pytest.raises(MappingRefused) as down:
        istem_listing(_admin(), client_factory=_down())
    assert down.value.http_status == 502 and "not answering" in down.value.message
    assert "AUTH_UNAVAILABLE" not in down.value.message and "CONNECT_FAILED" not in down.value.message


def test_search_falls_back_to_the_last_list(settings):
    from iic_booking.istem.equipment_mapping import search_istem_equipment

    apply_live(settings)
    _, factory = _fake(DEPT_LISTING)
    istem_listing(_admin(), client_factory=factory)
    source: dict = {}
    rows = search_istem_equipment(_admin(), "fluorescence", client_factory=_down(), source=source)
    assert [r["equipment_id"] for r in rows] == ["38750", "38760"] and source["live"] is False


def test_saving_while_istem_is_down_keeps_the_mapping_and_says_so(settings):
    from iic_booking.istem.equipment_mapping import save_mapping

    apply_live(settings)
    xrf = _equipment(_department("C"), "XRF")
    out = save_mapping(_admin(), xrf.pk, {"istem_equipment_id": "38750", "sample_count_input_key": "A"},
                       client_factory=_down())
    assert out["mapping"]["istem_equipment_id"] == "38750" and out["checked"] is False
    assert any("checked on I-STEM later" in w for w in out["warnings"])


def test_department_progress_lists_linked_istem_departments():
    from iic_booking.istem.models import IstemDepartmentMapping

    iic = _department("I")
    _equipment(iic, "XRF")
    IstemDepartmentMapping.objects.create(istem_department_id="164", department=iic)
    body = _api(_admin()).get(URL).data
    assert {d["id"]: d["istem_department_ids"] for d in body["departments"]}[iic.pk] == ["164"]
