"""Publication-to-facility mapping: aliases, acknowledgement extraction and scoring, evidence + mappings, the review queue,
OpenAlex author matching, API permissions and the feature flag. No network: readers and lookups are faked."""

import io
from decimal import Decimal
from unittest import mock

import pytest
from django.core.management import call_command

from iic_booking.equipment import publication_dedupe
from iic_booking.equipment.models import EquipmentCategory, EquipmentPublication, EquipmentPublicationClaim
from iic_booking.impact import author_linking
from iic_booking.impact import facility_mapping as fm
from iic_booking.impact import service
from iic_booking.impact.models import (
    AcknowledgementPolicy,
    AliasSource,
    AuthorLinkStatus,
    EquipmentFunding,
    EvidenceStatus,
    FacilityAlias,
    MappingStatus,
    PublicationEvidence,
    PublicationFacilityMapping,
    PublicationSuggestion,
    ResearcherAuthorCandidate,
    ReviewStatus,
)
from iic_booking.training.tests.conftest import client_for, make_equipment, world  # noqa: F401

pytestmark = pytest.mark.django_db
API = "/api/v1/impact"
ROR = "https://ror.org/000000000"


@pytest.fixture
def facility(world):
    """Chemistry hosts the FE-SEM (world.equipment) and a Raman spectrometer; Physics hosts the XRD."""
    world.dept.name, world.dept.code = "Central Analytical Facility", "CAF"
    world.dept.save()
    world.raman = make_equipment(world.dept, name="Confocal Raman Spectrometer", code="RAMAN")
    world.equipment.code = "FESEM"
    world.equipment.save()
    call_command("seed_facility_aliases", "--apply", stdout=io.StringIO())
    return world


ACK_PAPER = """Title of the paper
Thin Film Laboratory, Central Analytical Facility, Some Institute
Abstract. We grew films and measured them.
1. Introduction ... results ... discussion ...
Acknowledgements
The authors thank the Central Analytical Facility for the FESEM and Confocal Raman Spectrometer measurements.
We also thank DST for funding.
References
[1] A. Author, J. Things 1 (2020) 1.
""" + ("filler text. " * 200)


def _work(doi, oa=True, pdf="https://repo.example.org/paper.pdf"):
    loc = {"is_oa": True, "pdf_url": pdf, "landing_page_url": "https://repo.example.org/paper", "license": "cc-by"}
    return {"id": f"https://openalex.org/W{abs(hash(doi)) % 10000}", "doi": f"https://doi.org/{doi}", "title": "T",
            "publication_year": 2025, "open_access": {"is_oa": oa, "oa_status": "green" if oa else "closed"},
            "best_oa_location": loc if oa else None, "locations": [loc] if oa else [],
            "primary_location": {"source": {"display_name": "J. Films"}}}


def _suggest(user, doi, title="A thin film paper", year=2025):
    return PublicationSuggestion.objects.create(user=user, external_id=f"W-{doi}", doi=doi, title=title, year=year,
                                                authors="A. Author, B. Author")


# --------------------------------------------------------------------------- aliases
def test_aliases_come_from_names_codes_categories_policy_and_funding(world):
    world.dept.name, world.dept.code = "Central Analytical Facility", "CAF"
    world.dept.save()
    cat = EquipmentCategory.objects.create(name="Electron Microscopy")
    world.equipment.category = cat
    world.equipment.code = "FESEM"
    world.equipment.save()
    make_equipment(world.dept, name="EQ-20260730021949-A6BB50", code="EQ-20260730021949-A6BB50")  # generated: skipped
    AcknowledgementPolicy.objects.create(wording="Thank the CAF.", required_phrases=["Central Analytical Facility|CAF lab"])
    EquipmentFunding.objects.create(equipment=world.equipment, funder="DST", scheme="FIST", acknowledgement_phrase="DST-FIST")

    rows = {(r["department_id"], r["equipment_id"], r["phrase"], r["source"]) for r in fm.generate_aliases()}
    d, e = world.dept.pk, world.equipment.pk
    assert (d, None, "Central Analytical Facility", AliasSource.NAME) in rows
    assert (d, None, "CAF", AliasSource.CODE) in rows
    assert (d, None, "CAF lab", AliasSource.POLICY) in rows
    assert (None, e, "FESEM", AliasSource.CODE) in rows
    assert (None, e, "Electron Microscopy", AliasSource.CATEGORY) in rows
    assert (None, e, "DST-FIST", AliasSource.FUNDING) in rows
    assert not any("A6BB50" in r[2] for r in rows)
    assert (None, e, "FE-SEM", AliasSource.NAME) in rows

    out = io.StringIO()
    call_command("seed_facility_aliases", stdout=out)
    assert "DRY RUN" in out.getvalue() and not FacilityAlias.objects.exists()
    call_command("seed_facility_aliases", "--apply", stdout=io.StringIO())
    n = FacilityAlias.objects.count()
    FacilityAlias.objects.filter(phrase="CAF").update(is_active=False)  # an administrator switched it off
    call_command("seed_facility_aliases", "--apply", stdout=io.StringIO())
    assert FacilityAlias.objects.count() == n and not FacilityAlias.objects.get(phrase="CAF").is_active
    assert FacilityAlias.objects.get(phrase="CAF").case_sensitive


# --------------------------------------------------------------------------- extraction and scoring
def test_acknowledgement_section_is_cut_at_the_next_heading_and_affiliations_are_ignored():
    ack, how = fm.extract_acknowledgement(ACK_PAPER)
    assert how == "acknowledgement heading"
    assert ack.startswith("The authors thank the Central Analytical Facility") and "References" not in ack
    assert "Thin Film Laboratory" not in ack  # the author affiliation is not acknowledgement

    plain = "Intro. " * 200 + "We are grateful to the Central Analytical Facility for XPS. " + "Refs. " * 10
    ack2, how2 = fm.extract_acknowledgement(plain)
    assert how2.startswith("thank-you sentences") and "grateful to the Central Analytical Facility" in ack2
    assert fm.extract_acknowledgement("Nothing to see in this paper. " * 30)[0] == ""


def test_scoring_department_equipment_boost_codes_and_shared_phrases(facility):
    w = facility
    aliases = fm.load_aliases()
    by = {(m.level, m.equipment_id or m.department_id): m for m in fm.score_matches(
        "We thank the Central Analytical Facility for the FESEM and Confocal Raman Spectrometer.", aliases)}
    assert by[("DEPARTMENT", w.dept.pk)].confidence == 0.95
    assert by[("EQUIPMENT", w.equipment.pk)].confidence == 0.8  # code 0.6 + department acknowledged 0.2
    assert by[("EQUIPMENT", w.raman.pk)].confidence == 0.98  # name 0.9 + 0.2, capped
    assert "department/centre is acknowledged" in by[("EQUIPMENT", w.raman.pk)].rationale

    alone = fm.score_matches("Measurements were done on a FESEM.", aliases)
    assert [(m.level, m.confidence) for m in alone] == [("EQUIPMENT", 0.6)]  # below the 0.7 claim threshold
    assert fm.score_matches("We thank caf staff.", aliases) == []  # short code is case-sensitive

    # An instrument name without its department: recorded, capped below the claim threshold.
    named = fm.score_matches("Thanks for the Confocal Raman Spectrometer of the chemistry department.", aliases)
    assert [(m.equipment_id, m.confidence) for m in named] == [(w.raman.pk, 0.6)]
    assert "not enough for a claim" in named[0].rationale

    FacilityAlias.objects.create(equipment=w.equipment, phrase="spectrometer facility")
    FacilityAlias.objects.create(equipment=w.raman, phrase="spectrometer facility")
    shared = fm.score_matches("Thanks to the spectrometer facility.", fm.load_aliases())
    assert sorted(m.confidence for m in shared) == [0.54, 0.54]  # 0.9 x 0.6: ambiguous between two items


def test_institute_context_and_one_word_department_names(facility, settings):
    w = facility
    aliases = fm.load_aliases()
    text = "We thank the Central Analytical Facility for the Confocal Raman Spectrometer and the Department of Physics."
    assert {m.confidence for m in fm.score_matches(text, aliases) if m.level == "DEPARTMENT"} == {0.95, 0.75}

    settings.INSTITUTE_ROR_ID, settings.INSTITUTE_ACK_NAMES = ROR, "The Institute|TI"
    assert fm.institute_context(None, text) is False
    weak = fm.score_matches(text, aliases, fm.institute_context(None, text))
    assert all(m.confidence < 0.7 for m in weak) and "names the institute" in weak[0].rationale
    work = {"authorships": [{"institutions": [{"ror": ROR}]}]}
    assert fm.institute_context(work, text) is True
    assert fm.institute_context(None, text + " Work done at The Institute.") is True
    assert fm.institute_context(None, text + " Thanks, ti.") is False  # short name is case-sensitive
    settings.INSTITUTE_ROR_ID, settings.INSTITUTE_ACK_NAMES = "", ""
    assert fm.institute_context(None, text) is None
    assert "not checked" in fm.score_matches(text, aliases, None)[0].rationale


def test_check_paper_outcomes(facility):
    aliases = fm.load_aliases()
    assert fm.check_paper("10.1/a", "T", 2025, _work("10.1/a", oa=False), aliases).status == EvidenceStatus.NO_OPEN_ACCESS
    assert fm.check_paper("10.1/a", "T", 2025, None, aliases).detail == "Not found in OpenAlex."

    def broken(url):
        raise OSError("403")

    res = fm.check_paper("10.1/a", "T", 2025, _work("10.1/a"), aliases, reader=broken)
    assert res.status == EvidenceStatus.FETCH_FAILED and "repo.example.org: OSError" in res.detail
    blocked = "Please verify you are a human. Incident ID: 123. " * 5
    res = fm.check_paper("10.1/a", "T", 2025, _work("10.1/a"), aliases, reader=lambda u: blocked)
    assert res.status == EvidenceStatus.FETCH_FAILED and "blocked automated access" in res.detail
    res = fm.check_paper("10.1/a", "T", 2025, _work("10.1/a"), aliases, reader=lambda u: "Nothing here. " * 200)
    assert res.status == EvidenceStatus.NO_ACKNOWLEDGEMENT
    res = fm.check_paper("10.1/a", "T", 2025, _work("10.1/a"), aliases,
                         reader=lambda u: ACK_PAPER.replace("Central Analytical Facility for", "Another Centre for")
                         .replace("FESEM and Confocal Raman Spectrometer", "an optical bench"))
    assert res.status == EvidenceStatus.NO_MATCH
    res = fm.check_paper("10.1/a", "T", 2025, _work("10.1/a"), aliases, reader=lambda u: ACK_PAPER)
    assert res.status == EvidenceStatus.MATCHED and res.source_url == "https://repo.example.org/paper.pdf"
    assert res.snippet.startswith("The authors thank the Central Analytical Facility") and len(res.snippet) <= 600


def test_europe_pmc_full_text_is_used_for_pubmed_central_papers(facility):
    work = {**_work("10.1/p"), "ids": {"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/3484031"}}
    assert fm.open_access_urls(work)[0][0] == "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC3484031/fullTextXML"
    xml = ("<article><body><sec><title>Results</title><p>" + "Data. " * 400 + "</p></sec></body><back><ack><title>"
           "Acknowledgments</title><p>We thank the Central Analytical Facility &amp; staff.</p></ack><ref-list><ref>R1"
           "</ref></ref-list></back></article>")
    with mock.patch.object(fm, "_download", return_value=(xml.encode(), "application/xml")):
        res = fm.check_paper("10.1/p", "T", 2025, work, fm.load_aliases())
    assert res.status == EvidenceStatus.MATCHED and res.snippet == "We thank the Central Analytical Facility & staff."
    assert res.source_url.startswith("https://www.ebi.ac.uk/europepmc/")


def test_slow_or_oversized_downloads_are_abandoned(settings):
    class Resp:
        headers = {"Content-Type": "application/pdf"}

        def __init__(self, chunks):
            self.chunks = list(chunks)

        def read(self, n):
            return self.chunks.pop(0) if self.chunks else b""

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    settings.PUBLICATION_MAPPING_MAX_PDF_BYTES = 10
    with mock.patch.object(fm.urllib.request, "urlopen", return_value=Resp([b"%PDF-12345", b"678"])):
        with pytest.raises(ValueError, match="larger than 10 bytes"):
            fm._download("https://x.example/p.pdf")
    settings.PUBLICATION_MAPPING_MAX_PDF_BYTES = 1000
    ticks = iter([0.0, 61.0])
    with mock.patch.object(fm.urllib.request, "urlopen", return_value=Resp([b"%PDF-", b"more"])), \
            mock.patch.object(fm.time, "monotonic", side_effect=lambda: next(ticks)):
        with pytest.raises(TimeoutError):
            fm._download("https://x.example/p.pdf")


def test_line_numbered_preprints_and_hyphens_across_lines(facility):
    numbered = "\n".join(f"{700 + i} line {i} of the methods." for i in range(40))
    numbered += ("\n806 Acknowledgements We are grateful to the Central Analytical Facility for ICP-\n807 MS and ITC, "
                 "and for the micro-\n808 scopy.\n809 References")
    ack, _ = fm.extract_acknowledgement(numbered)
    assert "ICP-MS and ITC" in ack and "microscopy" in ack and "807" not in ack
    assert fm.normalise_text("12 samples were\nmeasured.\nText.").startswith("12 samples")  # ordinary text kept
    FacilityAlias.objects.create(equipment=facility.raman, phrase="ICP-MS")
    hits = fm.score_matches(ack, fm.load_aliases())
    assert any(m.equipment_id == facility.raman.pk and m.confidence >= 0.7 for m in hits)


def test_html_copies_are_read_too():
    page = "<html><nav>menu</nav><body><h2>Acknowledgments</h2><p>We thank the Lab &amp; Centre.</p><h2>References</h2></body></html>"
    text = fm.html_to_text(page)
    assert "menu" not in text and "We thank the Lab & Centre." in fm.extract_acknowledgement(text + "x " * 300)[0]


# --------------------------------------------------------------------------- saving
def _verify(user, **kw):
    works = {"10.1000/multi": _work("10.1000/multi"), "10.1000/closed": _work("10.1000/closed", oa=False)}
    return fm.verify_user(user, reader=lambda u: ACK_PAPER, works_lookup=lambda dois: works, **kw)


def test_dry_run_writes_nothing_and_apply_queues_the_match_for_review(facility):
    w = facility
    s1 = _suggest(w.faculty, "10.1000/multi")
    s2 = _suggest(w.faculty, "10.1000/closed", title="A paywalled paper")
    before = publication_dedupe.unique_publication_count()

    dry = _verify(w.faculty, apply=False)
    assert dry["papers"] == 2 and dry["for_review"] == 1 and dry["department_mappings"] == 1
    assert dry["by_status"] == {EvidenceStatus.MATCHED: 1, EvidenceStatus.NO_OPEN_ACCESS: 1}
    assert not PublicationEvidence.objects.exists() and not EquipmentPublicationClaim.objects.exists()

    done = _verify(w.faculty, apply=True)
    assert done["queued"] == 1 and len(done["queued_ids"]) == 1
    ev = PublicationEvidence.objects.get(doi="10.1000/multi")
    assert ev.status == EvidenceStatus.MATCHED and ev.review_status == ReviewStatus.PENDING
    assert ev.source_url and ev.acknowledgement_snippet
    assert PublicationEvidence.objects.get(doi="10.1000/closed").review_status == ReviewStatus.NONE
    maps = {(m.level, m.equipment_id): m for m in ev.mappings.all()}
    assert set(maps) == {("DEPARTMENT", None), ("EQUIPMENT", w.equipment.pk), ("EQUIPMENT", w.raman.pk)}
    assert all(m.status == MappingStatus.SUGGESTED and m.rationale and m.confidence >= Decimal("0.7") for m in maps.values())

    # Nothing is published or claimed before review; suggestions are untouched.
    assert not EquipmentPublicationClaim.objects.exists() and not EquipmentPublication.objects.exists()
    assert publication_dedupe.unique_publication_count() == before
    for sg in (s1, s2):
        sg.refresh_from_db()
        assert sg.status == "NEW"

    # Running again changes nothing (already checked); --recheck updates the same rows.
    assert _verify(w.faculty, apply=True)["papers"] == 0
    _verify(w.faculty, apply=True, recheck=True)
    assert PublicationEvidence.objects.count() == 2 and PublicationFacilityMapping.objects.count() == 3


@pytest.fixture
def reviewers(facility):
    from iic_booking.training.tests.conftest import make_user
    from iic_booking.users.models.user_type import UserType

    facility.dept_admin = make_user(user_type=UserType.DEPT_ADMIN, department=facility.dept, name="Dept Admin One")
    facility.other_dept_admin = make_user(user_type=UserType.DEPT_ADMIN, department=facility.other_dept, name="Dept Admin Two")
    return facility


def _queued(w, user=None, text=ACK_PAPER, doi="10.1000/multi"):
    user = user or w.faculty
    _suggest(user, doi)
    fm.verify_user(user, apply=True, reader=lambda u: text, works_lookup=lambda d: {doi: _work(doi)})
    return PublicationEvidence.objects.get(user=user, doi=doi)


def test_review_queue_is_scoped_to_main_admin_and_the_department_admin(reviewers):
    w = reviewers
    ev = _queued(w)
    url = f"{API}/publication-matches/"
    assert client_for(w.student).get(url).status_code == 403
    assert client_for(w.oic).get(url).status_code == 403
    admin = client_for(w.admin).get(url).data
    assert [r["id"] for r in admin["results"]] == [ev.pk] and admin["pending_count"] == 1
    row = admin["results"][0]
    assert row["researcher"]["name"] == w.faculty.name and row["evidence"]["acknowledgement_snippet"]
    assert row["evidence"]["source_url"] and row["can_review"] is True
    choices = {c["id"]: c for c in row["equipment_choices"]}
    assert choices[w.equipment.pk]["suggested"] and choices[w.raman.pk]["suggested"]
    assert [r["id"] for r in client_for(w.dept_admin).get(url).data["results"]] == [ev.pk]
    assert client_for(w.other_dept_admin).get(url).data["results"] == []
    assert client_for(w.dept_admin).get(f"{url}count/").data == {"pending_count": 1}


def test_approval_publishes_once_with_the_chosen_instruments_and_counts_for_recognition(reviewers):
    from iic_booking.cms import recognition

    w = reviewers
    ev = _queued(w)
    before = publication_dedupe.unique_publication_count()
    approve = f"{API}/publication-matches/{ev.pk}/approve/"
    bad = client_for(w.dept_admin).post(approve, {"equipment_ids": [w.other_equipment.pk]}, format="json")
    assert bad.status_code == 400 and bad.data["code"] == "equipment_scope"
    with mock.patch("iic_booking.communication.in_app.notify_in_app") as notify:
        r = client_for(w.dept_admin).post(approve, {"equipment_ids": [w.equipment.pk, w.raman.pk], "note": "Checked"}, format="json")
    assert r.status_code == 200 and r.data["review_status"] == "APPROVED"
    assert notify.call_args.kwargs["title"] == "Publication added" and notify.call_args.args[0] == [w.faculty]
    ev.refresh_from_db()
    claim = ev.claim
    assert claim.status == "approved" and claim.submitted_by_id == w.faculty.pk and claim.reviewed_by_id == w.dept_admin.pk
    assert set(claim.equipments.values_list("pk", flat=True)) == {w.equipment.pk, w.raman.pk}
    assert EquipmentPublication.objects.filter(doi="10.1000/multi").count() == 2  # on both equipment pages
    publication_dedupe.invalidate()
    assert publication_dedupe.unique_publication_count() == before + 1  # once on the Publications page
    assert set(ev.mappings.values_list("status", flat=True)) == {MappingStatus.APPROVED}
    assert recognition._approved_claims().filter(submitted_by=w.faculty).count() == 1  # Researcher of the Month
    again = client_for(w.admin).post(approve, {"equipment_ids": []}, format="json")
    assert again.status_code == 409 and again.data["code"] == "already_reviewed"


def test_department_level_approval_without_instruments_is_listed_on_the_publications_page(reviewers):
    w = reviewers
    ev = _queued(w)
    r = client_for(w.admin).post(f"{API}/publication-matches/{ev.pk}/approve/", {"equipment_ids": [w.raman.pk]}, format="json")
    assert r.status_code == 200
    statuses = {m.equipment_id: m.status for m in ev.mappings.all()}
    assert statuses[w.raman.pk] == MappingStatus.APPROVED and statuses[w.equipment.pk] == MappingStatus.NOT_SELECTED

    ev2 = _queued(w, user=w.faculty2, doi="10.1000/dept-only")
    before = publication_dedupe.unique_publication_count()
    assert client_for(w.admin).post(f"{API}/publication-matches/{ev2.pk}/approve/", {"equipment_ids": []}, format="json").status_code == 200
    ev2.refresh_from_db()
    assert ev2.claim.equipments.count() == 0 and not EquipmentPublication.objects.filter(doi="10.1000/dept-only").exists()
    publication_dedupe.invalidate()
    assert publication_dedupe.unique_publication_count() == before + 1
    found = publication_dedupe.query(q="thin film")["results"]
    assert any(p["doi"] == "10.1000/dept-only" and p["equipment"] == [] for p in found)


def test_papers_naming_another_departments_facilities_go_to_the_main_admin(reviewers):
    w = reviewers
    w.other_dept.name = "Physics Department"
    w.other_dept.save()
    call_command("seed_facility_aliases", "--apply", stdout=io.StringIO())
    text = ACK_PAPER.replace("We also thank DST for funding.", "We also thank the Physics Department for the XRD.")
    ev = _queued(w, text=text)
    assert {m.department_id for m in ev.mappings.filter(status=MappingStatus.SUGGESTED)} == {w.dept.pk, w.other_dept.pk}
    row = client_for(w.dept_admin).get(f"{API}/publication-matches/").data["results"][0]
    assert row["can_review"] is False and "Main Administrator" in row["review_blocked_reason"]
    denied = client_for(w.dept_admin).post(f"{API}/publication-matches/{ev.pk}/approve/", {"equipment_ids": []}, format="json")
    assert denied.status_code == 403
    assert client_for(w.admin).post(f"{API}/publication-matches/{ev.pk}/approve/",
                                    {"equipment_ids": [w.equipment.pk, w.other_equipment.pk]}, format="json").status_code == 200


def test_rejection_is_kept_and_a_later_check_never_reopens_a_decision(reviewers):
    w = reviewers
    ev = _queued(w)
    r = client_for(w.dept_admin).post(f"{API}/publication-matches/{ev.pk}/reject/", {"note": "Not our facility"}, format="json")
    assert r.status_code == 200 and r.data["review_status"] == "REJECTED"
    fm.verify_user(w.faculty, apply=True, recheck=True, reader=lambda u: ACK_PAPER,
                   works_lookup=lambda d: {"10.1000/multi": _work("10.1000/multi")})
    ev.refresh_from_db()
    assert ev.review_status == ReviewStatus.REJECTED and ev.review_note == "Not our facility"
    assert not EquipmentPublicationClaim.objects.exists()
    rejected = client_for(w.admin).get(f"{API}/publication-matches/?status=REJECTED").data["results"]
    assert [x["id"] for x in rejected] == [ev.pk]


def test_already_listed_equipment_and_weak_evidence_are_not_queued(facility):
    w = facility
    EquipmentPublication.objects.create(equipment=w.equipment, title="A thin film paper", doi="10.1000/multi", year=2025)
    ev = _queued(w)
    by_eq = {m.equipment_id: m for m in ev.mappings.filter(level="EQUIPMENT")}
    assert by_eq[w.equipment.pk].status == MappingStatus.BELOW_THRESHOLD and "already lists" in by_eq[w.equipment.pk].rationale
    assert by_eq[w.raman.pk].status == MappingStatus.SUGGESTED and ev.review_status == ReviewStatus.PENDING

    weak = ACK_PAPER.replace("the Central Analytical Facility for the FESEM and Confocal Raman Spectrometer", "a FESEM")
    ev2 = _queued(w, user=w.faculty2, text=weak)
    assert ev2.review_status == ReviewStatus.NONE
    assert set(ev2.mappings.values_list("status", flat=True)) == {MappingStatus.BELOW_THRESHOLD}


def test_reviewers_are_notified_once_each(reviewers):
    from iic_booking.impact import match_review

    w = reviewers
    ev = _queued(w)
    with mock.patch("iic_booking.communication.in_app.notify_in_app") as notify:
        assert match_review.notify_reviewers([ev.pk]) == 2  # the Main Admin and this department's admin
    told = [c.args[0][0] for c in notify.call_args_list]
    assert {u.pk for u in told} == {w.admin.pk, w.dept_admin.pk}
    assert all(c.kwargs["link"] == "/publication-matches" for c in notify.call_args_list)
    assert match_review.notify_reviewers([]) == 0


def test_verify_command_dry_run_and_dismissed_suggestions_are_skipped(facility):
    w = facility
    _suggest(w.faculty, "10.1000/multi")
    PublicationSuggestion.objects.create(user=w.faculty, external_id="W-x", doi="10.1000/x", title="Not mine", status="DISMISSED")
    out = io.StringIO()
    with mock.patch.object(fm, "openalex_works_by_doi", return_value={"10.1000/multi": _work("10.1000/multi")}), \
            mock.patch.object(fm, "read_full_text", return_value=ACK_PAPER):
        call_command("verify_publication_acknowledgements", "--user-id", str(w.faculty.pk), stdout=out)
    text = out.getvalue()
    assert "DRY RUN" in text and "papers=1" in text and "for review=1" in text and "Not mine" not in text
    assert not PublicationEvidence.objects.exists()


# --------------------------------------------------------------------------- author matching
def _author(aid, name, works=40, ror=ROR, orcid=None, alts=()):
    return {"id": f"https://openalex.org/{aid}", "display_name": name, "display_name_alternatives": list(alts),
            "orcid": f"https://orcid.org/{orcid}" if orcid else None, "works_count": works,
            "last_known_institutions": [{"ror": ror, "display_name": "The Institute"}]}


def test_name_scores():
    assert author_linking.name_score("Prof. Asha Rao", ["Asha Rao"])[0] == 1.0
    assert author_linking.name_score("Asha Rao", ["Rao Asha"])[0] == 0.95
    assert author_linking.name_score("Asha K Rao", ["A. K. Rao"])[0] == 0.75
    assert author_linking.name_score("Asha Rao", ["Vijay Rao"])[0] == 0.4
    assert author_linking.name_score("Asha Rao", ["Someone Else"])[0] == 0.0


def test_only_unambiguous_authors_are_linked_and_decisions_are_kept(world, settings):
    settings.INSTITUTE_ROR_ID = ROR
    single = [_author("A1", "Asha Rao"), _author("A2", "Vijay Rao")]
    cands = author_linking.link_user(world.faculty, apply=True, search=lambda n, r: single)
    assert [c["status"] for c in cands] == [AuthorLinkStatus.AUTO_LINKED]  # surname-only candidate dropped
    assert ResearcherAuthorCandidate.objects.get(user=world.faculty).status == AuthorLinkStatus.AUTO_LINKED

    split = [_author("B1", "B Iyer"), _author("B2", "B. Iyer", works=10)]
    author_linking.link_user(world.faculty2, apply=True, search=lambda n, r: split)
    rows = ResearcherAuthorCandidate.objects.filter(user=world.faculty2)
    assert {r.status for r in rows} == {AuthorLinkStatus.NEEDS_CONFIRMATION}  # two strong records: ask

    b1 = rows.get(openalex_author_id="B1")
    client = client_for(world.faculty2)
    assert client.post(f"{API}/author-candidates/{b1.pk}/decide/", {"confirm": True}, format="json").data["status"] == "CONFIRMED"
    assert client_for(world.faculty).post(f"{API}/author-candidates/{b1.pk}/decide/", {"confirm": False}, format="json").status_code == 403
    author_linking.link_user(world.faculty2, apply=True, search=lambda n, r: split)
    b1.refresh_from_db()
    assert b1.status == AuthorLinkStatus.CONFIRMED  # a later run never overrides a person's decision
    assert len(client.get(f"{API}/author-candidates/").data["results"]) == 2
    assert client_for(world.admin).get(f"{API}/author-candidates/?user_id={world.faculty2.pk}").data["results"]


def test_orcid_decides_and_past_affiliation_lowers_confidence(world, settings):
    settings.INSTITUTE_ROR_ID = ROR
    service.set_orcid(world.faculty, "0000-0002-1825-0097")
    authors = [_author("C1", "Asha Rao", orcid="0000-0002-1825-0097"), _author("C2", "Asha Rao", orcid="0000-0001-5109-3700")]
    cands = author_linking.link_user(world.faculty, apply=False, search=lambda n, r: authors)
    assert cands[0]["openalex_author_id"] == "C1" and cands[0]["status"] == AuthorLinkStatus.AUTO_LINKED
    assert all(c["openalex_author_id"] != "C2" for c in cands)  # different ORCID: 0.3, dropped
    past = author_linking.score_candidates(world.faculty2, [_author("D1", "B Iyer", ror="https://ror.org/other")], ROR)
    assert past[0]["confidence"] == 0.8 and "past affiliation" in past[0]["rationale"]
    settings.INSTITUTE_ROR_ID = ""
    with pytest.raises(author_linking.AuthorLinkError):
        author_linking.link_user(world.faculty, apply=False, search=lambda n, r: [])


# --------------------------------------------------------------------------- API and flag
def test_alias_api_permissions(facility):
    w = facility
    url = f"{API}/facility-aliases/"
    assert client_for(w.student).get(url).status_code == 403
    r = client_for(w.oic).post(url, {"equipment_id": w.equipment.pk, "phrase": "Gemini FESEM"}, format="json")
    assert r.status_code == 201 and r.data["source"] == "MANUAL"
    assert client_for(w.oic).post(url, {"equipment_id": w.other_equipment.pk, "phrase": "Big XRD"}, format="json").status_code == 403
    assert client_for(w.oic).post(url, {"department_id": w.dept.pk, "phrase": "CAF centre"}, format="json").status_code == 403
    assert client_for(w.admin).post(url, {"department_id": w.dept.pk, "phrase": "CAF centre"}, format="json").status_code == 201
    assert client_for(w.admin).post(url, {"department_id": w.dept.pk, "phrase": "caf centre"}, format="json").status_code == 409
    assert client_for(w.admin).post(url, {"phrase": "x y"}, format="json").data["code"] == "one_target"
    mine = client_for(w.oic).get(url).data["results"]
    assert mine and all(a["equipment"] and a["equipment"]["id"] == w.equipment.pk for a in mine)
    pk = r.data["id"]
    assert client_for(w.other_oic).patch(f"{url}{pk}/", {"is_active": False}, format="json").status_code == 403
    assert client_for(w.oic).patch(f"{url}{pk}/", {"is_active": False}, format="json").data["is_active"] is False
    assert client_for(w.oic).delete(f"{url}{pk}/").status_code == 204


def test_mappings_api_is_scoped_and_levels_are_distinct(facility):
    w = facility
    _suggest(w.faculty, "10.1000/multi")
    _verify(w.faculty, apply=True)
    url = f"{API}/publication-mappings/"
    assert len(client_for(w.admin).get(url).data["results"]) == 3
    assert [m["level"] for m in client_for(w.admin).get(f"{url}?level=DEPARTMENT").data["results"]] == ["DEPARTMENT"]
    assert {m["equipment"]["id"] for m in client_for(w.oic).get(url).data["results"]} == {w.equipment.pk}
    assert client_for(w.other_oic).get(url).data["results"] == []
    assert len(client_for(w.faculty).get(url).data["results"]) == 3  # the researcher's own paper
    assert client_for(w.faculty).get(url).data["results"][0]["evidence"]["source_url"]


def test_daily_scan_runs_only_when_the_flag_is_on(world, settings):
    settings.PUBLICATION_MAPPING_ENABLED = False
    with mock.patch.object(service, "weekly_mapping") as wm:
        assert service.daily_publication_scan() == {"skipped": "PUBLICATION_MAPPING_ENABLED is off"}
        wm.assert_not_called()
    assert "mapping" not in service.weekly_discovery()  # the weekly discovery no longer runs the mapping itself
    settings.PUBLICATION_MAPPING_ENABLED = True
    with mock.patch.object(service, "weekly_discovery", return_value={"checked": 0}) as wd, \
            mock.patch.object(service, "weekly_mapping", return_value={"papers_checked": 3, "queued_ids": [11]}) as wm, \
            mock.patch("iic_booking.impact.match_review.notify_reviewers", return_value=2) as nr:
        out = service.daily_publication_scan()
    wd.assert_called_once()
    wm.assert_called_once()
    nr.assert_called_once_with([11])
    assert out["reviewers_notified"] == 2 and out["mapping"] == {"papers_checked": 3}


def test_acronym_spelling_and_technique_aliases(world):
    from iic_booking.impact.facility_mapping import acronym_variants, name_variants

    assert acronym_variants("FE-SEM") == ["FESEM", "FE SEM", "SEM"]
    assert acronym_variants("FESEM") == ["FE-SEM", "SEM"]
    assert acronym_variants("HRTEM") == ["HR-TEM", "TEM"]
    assert acronym_variants("PXRD") == ["P-XRD", "XRD"]
    assert acronym_variants("TEM") == [] and acronym_variants("Raman") == []
    assert "ICP-MS" in acronym_variants("ICPMS-MS") and "ICP-MS" in acronym_variants("ICPMS")
    assert "XRD" in acronym_variants("GI-XRD")
    assert name_variants("Institute Instrumentation Centre") == ["Institute Instrumentation Center"]

    world.dept.name = "Central Instrumentation Centre"
    world.dept.save()
    tem = make_equipment(world.dept, name="Transmission Electron Microscope (TEM)", code="EQ-TEM-01")
    world.equipment.name, world.equipment.code = "FE-SEM", "FESEM01"
    world.equipment.save()
    rows = {(r["equipment_id"] or r["department_id"], r["phrase"]): r for r in fm.generate_aliases()}
    assert rows[(tem.pk, "TEM")]["source"] == AliasSource.DERIVED and rows[(tem.pk, "TEM")]["case_sensitive"]
    assert (tem.pk, "Transmission Electron Microscope") in rows
    for phrase in ("FESEM", "FE SEM", "SEM"):
        assert rows[(world.equipment.pk, phrase)]["source"] == AliasSource.DERIVED
    assert rows[(world.dept.pk, "Central Instrumentation Center")]["source"] == AliasSource.DERIVED

    call_command("seed_facility_aliases", "--apply", stdout=io.StringIO())
    hits = {m.equipment_id or m.department_id: m for m in fm.score_matches(
        "We thank the Central Instrumentation Center for SEM and TEM measurements.", fm.load_aliases())}
    assert hits[world.dept.pk].confidence == 0.95
    assert hits[world.equipment.pk].confidence == 0.8 and hits[tem.pk].confidence == 0.8  # 0.6 + department named
    assert not fm.score_matches("We used a sem image.", fm.load_aliases())  # acronyms match their case only


def test_several_instruments_of_one_kind_need_the_model_name(world):
    """Four FE-SEMs: "FESEM" alone is ambiguous (department-level suggestion, the reviewer picks); the model picks one."""
    world.dept.name = "Central Instrumentation Centre"
    world.dept.save()
    a = make_equipment(world.dept, name="Field Emission Scanning Electron Microscope (FESEM)-APREO", code="APREO")
    g = make_equipment(world.dept, name="Field Emission Scanning Electron Microscope (FESEM)-GEMINI 300", code="GEM300")
    u = make_equipment(world.dept, name="Field Emission Scanning Electron Microscope (FESEM)-ULTRA", code="ULTRA")
    call_command("seed_facility_aliases", "--apply", stdout=io.StringIO())
    assert not FacilityAlias.objects.filter(equipment=u, phrase__iexact="ULTRA", source=AliasSource.DERIVED).exists()
    aliases = fm.load_aliases()
    vague = {m.equipment_id: m.confidence for m in fm.score_matches(
        "We thank the Central Instrumentation Centre for the FESEM facility.", aliases) if m.equipment_id}
    assert set(vague) >= {a.pk, g.pk, u.pk} and all(c < 0.7 for c in vague.values())
    exact = {m.equipment_id: m.confidence for m in fm.score_matches(
        "We thank the Central Instrumentation Centre for the Zeiss Gemini 300 FESEM.", aliases) if m.equipment_id}
    assert exact[g.pk] >= 0.7 and exact[a.pk] < 0.7
    apreo = {m.equipment_id: m.confidence for m in fm.score_matches(
        "SEM images were taken on an Apreo at the Central Instrumentation Center.", aliases) if m.equipment_id}
    assert apreo[a.pk] >= 0.7


# --------------------------------------------------------------------------- department names never become instrument aliases
ACK_CENTRE_ONLY = ACK_PAPER.replace(
    "The authors thank the Central Analytical Facility for the FESEM and Confocal Raman Spectrometer measurements.",
    "We thank the technicians of the Central Analytical Facility (CAF) for support with the analyses.")


def test_instrument_named_after_its_centre_does_not_take_the_centre_code(facility):
    w = facility
    lab = make_equipment(w.dept, name="Electronic Repair lab (CAF)", code="ERL1")
    rows = {(r["equipment_id"], r["phrase"]) for r in fm.generate_aliases()}
    assert (lab.pk, "CAF") not in rows and (lab.pk, "Electronic Repair lab") in rows
    assert (None, "CAF") in rows  # still the department's code

    # An alias seeded before the fix pulls the instrument into a centre-only acknowledgement ...
    bad = FacilityAlias.objects.create(equipment=lab, phrase="CAF", source=AliasSource.DERIVED, case_sensitive=True)
    manual = FacilityAlias.objects.create(equipment=lab, phrase="Central Analytical Facility", source=AliasSource.MANUAL)
    ev = _queued(w, text=ACK_CENTRE_ONLY, doi="10.1000/centre")
    assert ev.review_status == ReviewStatus.PENDING
    assert ev.mappings.filter(equipment=lab).exists()
    manual.delete()

    # ... the dry run reports it and changes nothing; --apply switches it off and re-matches the waiting paper.
    out = io.StringIO()
    call_command("seed_facility_aliases", stdout=out)
    assert "switched off: 1" in out.getvalue() and f"evidence {ev.pk}" in out.getvalue()
    bad.refresh_from_db()
    assert bad.is_active and ev.mappings.filter(equipment=lab).exists()

    call_command("seed_facility_aliases", "--apply", stdout=io.StringIO())
    bad.refresh_from_db()
    ev.refresh_from_db()
    assert not bad.is_active
    assert not ev.mappings.filter(equipment=lab).exists()
    assert ev.mappings.filter(level="DEPARTMENT", department=w.dept).exists()
    assert ev.review_status == ReviewStatus.PENDING


def test_rescore_never_touches_decided_papers(facility):
    w = facility
    ev = _queued(w)
    ev.review_status = ReviewStatus.REJECTED
    ev.save()
    n = ev.mappings.count()
    FacilityAlias.objects.filter(equipment=w.raman).update(is_active=False)
    assert fm.rescore_pending(apply=True) == []
    assert ev.mappings.count() == n


def test_recheck_removes_mappings_the_paper_no_longer_supports(facility):
    w = facility
    ev = _queued(w)
    assert ev.mappings.filter(equipment=w.raman).exists()
    FacilityAlias.objects.filter(equipment=w.raman).update(is_active=False)
    _verify(w.faculty, apply=True, recheck=True)
    assert not ev.mappings.filter(equipment=w.raman).exists()
