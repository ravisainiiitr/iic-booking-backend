"""Research impact: ORCID validation and publication discovery, acknowledgement check, funding lines, funder report."""

import io
import json
from datetime import date
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from iic_booking.equipment.models import EquipmentPublication, EquipmentPublicationClaim
from iic_booking.impact import service
from iic_booking.impact.models import EquipmentFunding, PublicationSuggestion, ResearcherOrcid
from iic_booking.training.tests.conftest import client_for, make_equipment, world  # noqa: F401

pytestmark = pytest.mark.django_db
API = "/api/v1/impact"
VALID = "0000-0002-1825-0097"  # ORCID's documented example iD


# --------------------------------------------------------------------------- ORCID
def test_orcid_is_validated_normalised_and_unique(world):
    url = f"{API}/me/orcid/"
    assert client_for(world.student).put(url, {"orcid": "0000-0002-1825-0098"}, format="json").data["code"] == "invalid_orcid"
    assert client_for(world.student).put(url, {"orcid": "12345"}, format="json").status_code == 400
    resp = client_for(world.student).put(url, {"orcid": f"https://orcid.org/{VALID.replace('-', '')}"}, format="json")
    assert resp.status_code == 200 and resp.data["orcid"] == VALID
    assert client_for(world.student2).put(url, {"orcid": VALID}, format="json").status_code == 409
    assert service.normalise_orcid("0000-0001-5109-3700") == "0000-0001-5109-3700"
    assert client_for(world.student).delete(url).data["orcid"] == ""


def _works():
    return [
        {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1000/ALREADY", "title": "Claimed already",
         "publication_year": 2025},
        {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.1000/new.paper", "title": "New paper on XRD",
         "publication_year": 2026, "primary_location": {"source": {"display_name": "J. Materials"}},
         "authorships": [{"author": {"display_name": "Student One"}}, {"author": {"display_name": "Asha Rao"}}]},
        {"id": "https://openalex.org/W3", "doi": None, "title": "", "publication_year": 2026},
    ]


def test_discovery_suggests_unclaimed_papers_once_and_respects_cooldown(world):
    EquipmentPublication.objects.create(equipment=world.equipment, title="Claimed already", doi="10.1000/already", year=2025)
    service.set_orcid(world.student, VALID)
    with mock.patch.object(service, "_fetch_openalex", return_value=_works()) as fetch:
        resp = client_for(world.student).post(f"{API}/me/suggestions/check/", {}, format="json")
        assert resp.status_code == 200 and resp.data == {"found": 3, "added": 1}
        assert fetch.call_args[0][0] == VALID  # only the ORCID iD is sent
        assert client_for(world.student).post(f"{API}/me/suggestions/check/", {}, format="json").status_code == 429
        assert service.discover(world.student, force=True)["added"] == 0
    rows = client_for(world.student).get(f"{API}/me/suggestions/").data["results"]
    assert [(r["title"], r["doi"], r["journal"]) for r in rows] == [("New paper on XRD", "10.1000/new.paper", "J. Materials")]
    s = PublicationSuggestion.objects.get()
    assert client_for(world.student2).post(f"{API}/me/suggestions/{s.pk}/dismiss/", {}, format="json").status_code == 403
    # Claimed through the usual form: the suggestion is marked claimed.
    EquipmentPublicationClaim.objects.create(submitted_by=world.student, title="New paper on XRD", doi="10.1000/NEW.PAPER")
    assert client_for(world.student).get(f"{API}/me/suggestions/").data["results"] == []
    s.refresh_from_db()
    assert s.status == "CLAIMED"


def test_discovery_failure_is_reported_not_raised(world):
    service.set_orcid(world.student, VALID)
    with mock.patch.object(service, "_fetch_openalex", side_effect=OSError("timeout")):
        resp = client_for(world.student).post(f"{API}/me/suggestions/check/", {}, format="json")
    assert resp.status_code == 502 and resp.data["code"] == "lookup_failed"
    assert ResearcherOrcid.objects.get(user=world.student).last_error == "timeout"
    assert client_for(world.student2).post(f"{API}/me/suggestions/check/", {}, format="json").data["code"] == "orcid_required"


class _Resp:
    def __init__(self, body):
        self.body = json.dumps(body).encode("utf-8")

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _orcid_record(*works):
    """pub.orcid.org /works body: (year, doi) pairs; doi None = no DOI listed."""
    groups = []
    for year, doi in works:
        ids = [{"external-id-type": "doi", "external-id-value": doi}] if doi else []
        groups.append({"external-ids": {"external-id": ids},
                       "work-summary": [{"publication-date": {"year": {"value": str(year)}} if year else None}]})
    return {"group": groups}


def _router(orcid_body, openalex_pages, urls):
    pages = iter(openalex_pages)

    def fake_open(req, timeout):
        urls.append(req.full_url)
        if "pub.orcid.org" in req.full_url:
            if isinstance(orcid_body, Exception):
                raise orcid_body
            return _Resp(orcid_body)
        return _Resp(next(pages))

    return fake_open


def test_fetch_takes_papers_from_the_orcid_record_and_details_from_openalex(settings):
    settings.OPENALEX_MAILTO = ""
    record = _orcid_record((2025, "10.1000/A"), (2023, "https://doi.org/10.1000/b"), (2015, "10.1000/old"),
                           (None, "10.1000/undated"), (2024, None))
    details = {"results": [
        {"id": "W-a", "doi": "https://doi.org/10.1000/a", "type": "article"},
        {"id": "W-b", "doi": "https://doi.org/10.1000/b", "type": "peer-review"},
        {"id": "W-u", "doi": "https://doi.org/10.1000/undated", "type": "review"},
    ]}
    linked = {"meta": {"next_cursor": None}, "results": [{"id": "W-a", "type": "article"}, {"id": "W-x", "type": "article"}]}
    urls = []
    with mock.patch.object(service.urllib.request, "urlopen", side_effect=_router(record, [details, linked], urls)):
        works = service._fetch_openalex(VALID, date(2022, 1, 1))
    assert [w["id"] for w in works] == ["W-a", "W-u", "W-x"]  # peer-review dropped, W-a not repeated
    assert urls[0] == f"https://pub.orcid.org/v3.0/{VALID}/works"
    assert "doi%3A10.1000%2Fa%7C10.1000%2Fb%7C10.1000%2Fundated" in urls[1]  # old paper skipped
    assert f"author.orcid%3Ahttps%3A%2F%2Forcid.org%2F{VALID}" in urls[2] and len(urls) == 3


def test_fetch_batches_many_dois():
    record = _orcid_record(*[(2025, f"10.1000/p{i}") for i in range(95)])
    urls = []
    pages = [{"results": [{"id": f"W{i}", "type": "article"}]} for i in range(3)] + [{"results": []}]
    with mock.patch.object(service.urllib.request, "urlopen", side_effect=_router(record, pages, urls)):
        assert len(service._fetch_openalex(VALID, date(2022, 1, 1))) == 3
    assert len(urls) == 5  # ORCID + ceil(95 / 40) DOI requests + OpenAlex's own ORCID link


def test_fetch_reads_every_openalex_page_when_the_orcid_record_has_no_dois(settings):
    settings.OPENALEX_MAILTO = ""
    pages = [
        {"meta": {"next_cursor": "c2"}, "results": [{"id": f"W{i}", "type": "article"} for i in range(200)]},
        {"meta": {"next_cursor": "c3"}, "results": [
            {"id": "W-review", "type": "review"}, {"id": "W-peer", "type": "peer-review"},
            {"id": "W-erratum", "type": "erratum"}, {"id": "W-chapter", "type": "book-chapter"},
            {"id": "W-untyped"},
        ]},
        {"meta": {"next_cursor": None}, "results": []},
    ]
    urls = []
    with mock.patch.object(service.urllib.request, "urlopen", side_effect=_router({"group": []}, pages, urls)):
        works = service._fetch_openalex(VALID, date(2022, 1, 1))
    ids = [w["id"] for w in works]
    assert len(ids) == 203 and "W-peer" not in ids and "W-erratum" not in ids
    assert {"W-review", "W-chapter", "W-untyped"} <= set(ids)
    assert "cursor=%2A" in urls[1] and "cursor=c2" in urls[2] and "cursor=c3" in urls[3]
    assert all(f"orcid.org%2F{VALID}" in u for u in urls[1:])


def test_fetch_falls_back_when_orcid_is_unreachable_and_stops_at_the_page_limit():
    page = {"meta": {"next_cursor": "more"}, "results": [{"id": "W", "type": "article"}]}
    urls = []
    pages = [page] * (service.OPENALEX_MAX_PAGES + 3)
    with mock.patch.object(service.urllib.request, "urlopen", side_effect=_router(OSError("down"), pages, urls)):
        assert len(service._fetch_openalex(VALID, date(2022, 1, 1))) == service.OPENALEX_MAX_PAGES
    assert len(urls) == 1 + service.OPENALEX_MAX_PAGES


def _aff_work(i, aff, doi=None):
    return {"id": f"https://openalex.org/W{i}", "doi": f"https://doi.org/{doi or f'10.1000/w{i}'}", "type": "article",
            "title": f"Paper {i}", "publication_year": 2020 + i,
            "authorships": [{"author": {"display_name": "A. Author"}, "raw_affiliation_strings": [aff]}]}


def test_import_command_dry_runs_then_suggests_only_matching_papers(world):
    EquipmentPublication.objects.create(equipment=world.equipment, title="Paper 3", doi="10.1000/w3", year=2023)
    works = [
        _aff_work(1, "Thin Film Lab, Central Instrumentation Centre, Some University"),
        _aff_work(2, "Department of Physics, Some University"),
        _aff_work(3, "Nanoscience Lab, Central Instrumentation Center"),  # already on the equipment page
        {**_aff_work(4, ""), "authorships": [{"institutions": [{"display_name": "Central Instrumentation Centre"}]}]},
    ]
    args = ["--user-id", str(world.faculty.pk), "--orcid", VALID, "--affiliation", r"central\s+instrumentation\s+cent(re|er)"]
    out = io.StringIO()
    with mock.patch.object(service, "_fetch_openalex", return_value=works) as fetch:
        call_command("import_orcid_publications", *args, stdout=out)
        assert fetch.call_args[0] == (VALID, date(1900, 1, 1))
        assert "affiliation matches=3" in out.getvalue() and "Dry run" in out.getvalue()
        assert not ResearcherOrcid.objects.exists() and not PublicationSuggestion.objects.exists()

        call_command("import_orcid_publications", *args, "--apply", stdout=io.StringIO())
        call_command("import_orcid_publications", *args, "--apply", stdout=(again := io.StringIO()))
    assert ResearcherOrcid.objects.get(user=world.faculty).orcid == VALID
    assert sorted(PublicationSuggestion.objects.filter(user=world.faculty).values_list("title", flat=True)) == ["Paper 1", "Paper 4"]
    assert "0 new suggestion(s)" in again.getvalue()


def test_import_command_refuses_an_orcid_owned_by_someone_else(world):
    service.set_orcid(world.student, VALID)
    with pytest.raises(CommandError, match="another account"):
        call_command("import_orcid_publications", "--user-id", str(world.faculty.pk), "--orcid", VALID,
                     "--affiliation", "x", stdout=io.StringIO())


# --------------------------------------------------------------------------- acknowledgement
def test_acknowledgement_policy_and_check(world):
    url = f"{API}/acknowledgement/"
    body = {"wording": "The authors thank the Institute Instrumentation Centre for the XRD facility.",
            "required_phrases": ["Institute Instrumentation Centre|IIC"], "include_equipment_name": True}
    assert client_for(world.oic).put(url, body, format="json").status_code == 403
    assert client_for(world.admin).put(url, body, format="json").status_code == 200
    assert client_for(world.student).get(url).data["required_phrases"] == ["Institute Instrumentation Centre|IIC"]
    EquipmentFunding.objects.create(equipment=world.equipment, funder="DST", scheme="FIST",
                                    acknowledgement_phrase="DST-FIST|FIST programme")
    check = f"{API}/acknowledgement/check/"
    ids = [world.equipment.pk]
    full = client_for(world.student).post(check, {"text": f"We thank IIC for the {world.equipment.name} under the DST - FIST programme.",
                                                  "equipment_ids": ids}, format="json").data
    assert full["result"] == "COMPLETE" and not full["missing"]
    part = client_for(world.student).post(check, {"text": "Thanks to the Institute Instrumentation Centre.", "equipment_ids": ids},
                                          format="json").data
    assert part["result"] == "PARTIAL" and {m["for"] for m in part["missing"]} == {"DST FIST", "Instrument"}
    assert client_for(world.student).post(check, {"text": "", "equipment_ids": ids}, format="json").data["result"] == "MISSING"

    claim = EquipmentPublicationClaim.objects.create(submitted_by=world.student, title="Paper")
    claim.equipments.add(world.equipment)
    stored = client_for(world.student).post(check, {"text": "Thanks IIC", "claim_id": claim.pk}, format="json")
    assert stored.status_code == 200 and stored.data["stored"] and stored.data["result"] == "PARTIAL"
    assert client_for(world.oic).post(check, {"text": "x", "claim_id": claim.pk}, format="json").status_code == 200
    assert client_for(world.student2).post(check, {"text": "x", "claim_id": claim.pk}, format="json").status_code == 403


# --------------------------------------------------------------------------- funding lines
def test_funding_lines_by_oic_and_admin_only(world):
    url = f"{API}/funding/"
    body = {"equipment_id": world.equipment.pk, "funder": "DST", "scheme": "FIST", "sanction_reference": "SR/FST/2021/1",
            "amount": "12500000", "sanction_date": "2021-03-15"}
    assert client_for(world.other_oic).post(url, body, format="json").status_code == 403
    assert client_for(world.operator).post(url, body, format="json").status_code == 403
    assert client_for(world.oic).post(url, {**body, "amount": "-1"}, format="json").status_code == 400
    assert client_for(world.oic).post(url, {**body, "funder": ""}, format="json").status_code == 400
    resp = client_for(world.oic).post(url, body, format="json")
    assert resp.status_code == 201 and resp.data["amount"] == 12500000.0
    assert client_for(world.student).get(url).status_code == 403
    assert [r["id"] for r in client_for(world.oic).get(url).data["results"]] == [resp.data["id"]]
    assert client_for(world.other_oic).get(url).data["results"] == []
    patch = client_for(world.oic).patch(f"{url}{resp.data['id']}/", {"is_active": False}, format="json")
    assert patch.status_code == 200 and patch.data["is_active"] is False


# --------------------------------------------------------------------------- funder report
def _usage(rows):
    def fake(date_from, date_to, equipment_ids, include_revenue=True):
        return {
            "equipment": [r for r in rows if r["equipment_id"] in equipment_ids],
            "financial": {"revenue_by_equipment": [{"equipment_id": r["equipment_id"], "total": r["_rev"]} for r in rows]},
        }

    return fake


def test_funder_report_groups_counts_unique_papers_and_exports(world):
    year = timezone.localdate().year
    eq2 = make_equipment(world.dept, name="Raman", code="RAMAN1")
    from iic_booking.equipment.models import EquipmentManager

    EquipmentManager.objects.create(equipment=eq2, manager=world.oic)
    EquipmentFunding.objects.create(equipment=world.equipment, funder="DST", scheme="FIST", amount=Decimal("100"))
    EquipmentFunding.objects.create(equipment=eq2, funder="DST", scheme="FIST", amount=Decimal("50"))
    other = world.other_equipment
    # One paper on both instruments (same DOI) counts once for the group; an old paper is outside the period.
    EquipmentPublication.objects.create(equipment=world.equipment, title="Shared paper", doi="10.1021/shared", year=year,
                                        impact_factor=Decimal("4.5"))
    EquipmentPublication.objects.create(equipment=eq2, title="Shared paper", doi="https://doi.org/10.1021/SHARED", year=year)
    EquipmentPublication.objects.create(equipment=eq2, title="Old", doi="10.1021/old", year=year - 6)
    rows = [
        {"equipment_id": world.equipment.pk, "completed_in_period": 10, "total_booking_hours": 25.5, "distinct_users_internal": 4,
         "distinct_users_external": 1, "total_samples": 30, "blocked_hours": 2, "other_disruption_hours": 1, "_rev": "1000.00"},
        {"equipment_id": eq2.pk, "completed_in_period": 5, "total_booking_hours": 10, "distinct_users_internal": 2,
         "distinct_users_external": 0, "total_samples": 12, "_rev": "500"},
        {"equipment_id": other.pk, "completed_in_period": 1, "total_booking_hours": 1, "_rev": "0"},
    ]
    url = f"{API}/report/"
    params = {"date_from": f"{year}-01-01", "date_to": f"{year}-12-31"}
    with mock.patch("iic_booking.equipment.reports.get_equipment_report_data", side_effect=_usage(rows)):
        assert client_for(world.student).get(url, params).status_code == 403
        rep = client_for(world.admin).get(url, params).data
        dst = next(g for g in rep["groups"] if g["funder"] == "DST")
        t = dst["totals"]
        assert (t["equipment"], t["bookings"], t["booking_hours"], t["samples"], t["revenue"]) == (2, 15, 35.5, 42, 1500.0)
        assert t["publications_unique"] == 1 and t["impact_factor_sum"] == 4.5 and t["sanctioned_amount"] == 150.0
        assert rep["groups"][-1]["funder"] == service.UNFUNDED
        assert other.pk in {e["equipment_id"] for e in rep["groups"][-1]["equipment"]}
        # An OIC sees only their own instruments; a funder filter drops the unfunded group.
        mine = client_for(world.oic).get(url, {**params, "funder": "dst"}).data
        assert [g["funder"] for g in mine["groups"]] == ["DST"] and mine["equipment_count"] == 2
        assert client_for(world.other_oic).get(url, {**params, "funder": "DST"}).data["groups"] == []
        csv_resp = client_for(world.admin).get(url, {**params, "export": "csv"})
        assert csv_resp.status_code == 200 and csv_resp["Content-Type"].startswith("text/csv")
        assert b"Bookings completed" in csv_resp.content and b"RAMAN1" in csv_resp.content
        xlsx = client_for(world.admin).get(url, {**params, "export": "xlsx"})
        assert xlsx.status_code == 200 and xlsx.content[:2] == b"PK"
        assert client_for(world.admin).get(url, {"date_from": f"{year}-12-31", "date_to": f"{year}-01-01"}).status_code == 400
