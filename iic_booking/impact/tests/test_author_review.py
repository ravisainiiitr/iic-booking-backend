"""Confirming lookalike OpenAlex author records: who may decide, what changes, and the follow-up scan."""

from unittest import mock

import pytest

from iic_booking.impact import author_review
from iic_booking.impact.models import AuthorLinkStatus, ResearcherAuthorCandidate
from iic_booking.training.tests.conftest import client_for, make_user, world  # noqa: F401
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db
API = "/api/v1/impact/author-confirmations/"


def _cand(user, oa_id, status=AuthorLinkStatus.NEEDS_CONFIRMATION, works=10, conf="0.9"):
    return ResearcherAuthorCandidate.objects.create(
        user=user, openalex_author_id=oa_id, display_name=user.name, works_count=works, confidence=conf,
        institutions=["Indian Institute of Technology Roorkee"], rationale="same name", status=status)


@pytest.fixture
def lookalikes(world):
    w = world
    w.dept_admin = make_user(user_type=UserType.DEPT_ADMIN, department=w.dept, name="Dept Admin One")
    w.a1, w.a2 = _cand(w.faculty, "A1"), _cand(w.faculty, "A2", works=2)
    w.b1 = _cand(w.faculty2, "B1")
    w.b_auto = _cand(w.faculty2, "B0", status=AuthorLinkStatus.AUTO_LINKED)
    return w


def test_queue_is_scoped_main_admin_all_dept_admin_own_faculty(lookalikes):
    w = lookalikes
    body = client_for(w.admin).get(API).json()
    ids = [r["researcher"]["id"] for r in body["results"]]
    assert ids == [w.faculty.pk, w.faculty2.pk]  # not yet linked first
    assert body["waiting_count"] == 1  # faculty2 already has an automatic link
    assert client_for(w.admin).get(API + "?unlinked=1").json()["count"] == 1

    mine = client_for(w.dept_admin).get(API).json()
    assert [r["researcher"]["id"] for r in mine["results"]] == [w.faculty.pk]
    row = mine["results"][0]
    assert {c["id"] for c in row["waiting"]} == {w.a1.pk, w.a2.pk} and row["can_decide"]

    for u in (w.faculty, w.oic, w.operator):
        assert client_for(u).get(API).status_code == 403
        assert client_for(u).get(API + "count/").json() == {"waiting_count": 0}


def test_researcher_sees_and_decides_only_their_own(lookalikes):
    w = lookalikes
    mine = client_for(w.faculty).get(API + "mine/").json()
    assert {c["id"] for c in mine["waiting"]} == {w.a1.pk, w.a2.pk}
    assert client_for(w.faculty).post(f"{API}{w.faculty2.pk}/", {"confirm": [w.b1.pk]}, format="json").status_code == 403
    assert client_for(w.dept_admin).post(f"{API}{w.faculty2.pk}/", {"confirm": [w.b1.pk]}, format="json").status_code == 403
    # another researcher's record id cannot be slipped in
    r = client_for(w.faculty).post(f"{API}{w.faculty.pk}/", {"confirm": [w.b1.pk]}, format="json")
    assert r.status_code == 404
    w.b1.refresh_from_db()
    assert w.b1.status == AuthorLinkStatus.NEEDS_CONFIRMATION


def test_confirming_starts_the_follow_up_and_none_of_these_rejects(lookalikes, django_capture_on_commit_callbacks):
    w = lookalikes
    with mock.patch("iic_booking.impact.tasks.impact_author_confirmed_task.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            r = client_for(w.dept_admin).post(f"{API}{w.faculty.pk}/", {"confirm": [w.a1.pk], "reject": [w.a2.pk]},
                                              format="json")
    assert r.status_code == 200 and r.json()["scan_started"]
    delay.assert_called_once_with(w.faculty.pk)
    w.a1.refresh_from_db()
    w.a2.refresh_from_db()
    assert w.a1.status == AuthorLinkStatus.CONFIRMED and w.a1.decided_by == w.dept_admin
    assert w.a2.status == AuthorLinkStatus.REJECTED
    assert [c["id"] for c in r.json()["linked"]] == [w.a1.pk]
    assert client_for(w.admin).get(API).json()["waiting_count"] == 0

    # "None of these": reject everything waiting; no scan.
    with mock.patch("iic_booking.impact.tasks.impact_author_confirmed_task.delay") as delay:
        with django_capture_on_commit_callbacks(execute=True):
            r = client_for(w.admin).post(f"{API}{w.faculty2.pk}/", {"reject": [w.b1.pk]}, format="json")
    assert r.status_code == 200 and not r.json()["scan_started"]
    delay.assert_not_called()

    # Automatic links are not changed here; bad input is refused.
    client_for(w.admin).post(f"{API}{w.faculty2.pk}/", {"reject": [w.b_auto.pk]}, format="json")
    w.b_auto.refresh_from_db()
    assert w.b_auto.status == AuthorLinkStatus.AUTO_LINKED
    assert client_for(w.admin).post(f"{API}{w.faculty.pk}/", {}, format="json").status_code == 400
    assert client_for(w.admin).post(f"{API}{w.faculty.pk}/", {"confirm": ["x"]}, format="json").status_code == 400
    both = {"confirm": [w.a1.pk], "reject": [w.a1.pk]}
    assert client_for(w.admin).post(f"{API}{w.faculty.pk}/", both, format="json").status_code == 400


def test_decisions_survive_a_later_automatic_search(lookalikes, settings):
    from iic_booking.impact import author_linking

    w = lookalikes
    settings.INSTITUTE_ROR_ID = "https://ror.org/000000000"
    author_review.decide(w.admin, w.faculty, confirm_ids=[w.a2.pk], reject_ids=[w.a1.pk])
    fake = [{"id": "https://openalex.org/A1", "display_name": w.faculty.name, "works_count": 10, "orcid": None,
             "last_known_institutions": [{"ror": settings.INSTITUTE_ROR_ID, "display_name": "IITR"}],
             "affiliations": []}]
    author_linking.link_user(w.faculty, apply=True, search=lambda name, ror: fake)
    w.a1.refresh_from_db()
    w.a2.refresh_from_db()
    assert w.a1.status == AuthorLinkStatus.REJECTED and w.a2.status == AuthorLinkStatus.CONFIRMED


def test_recent_works_are_fetched_once_and_scoped(lookalikes):
    from django.core.cache import cache

    w = lookalikes
    cache.clear()
    body = {"results": [{"display_name": "A paper", "publication_year": 2025, "doi": "https://doi.org/10.1/x",
                         "primary_location": {"source": {"display_name": "J. Things"}}}]}
    with mock.patch("iic_booking.impact.openalex.get_json", return_value=body) as get:
        url = f"/api/v1/impact/author-candidates/{w.a1.pk}/works/"
        r = client_for(w.faculty).get(url)
        assert r.status_code == 200
        assert r.json()["results"] == [{"title": "A paper", "year": 2025, "journal": "J. Things", "doi": "10.1/x"}]
        client_for(w.dept_admin).get(url)
        assert get.call_count == 1
    assert client_for(w.faculty2).get(f"/api/v1/impact/author-candidates/{w.a1.pk}/works/").status_code == 403


def test_follow_up_discovers_then_checks_only_when_mapping_is_on(lookalikes, settings):
    w = lookalikes
    with mock.patch("iic_booking.impact.author_linking.discover_linked", return_value={"found": 3, "added": 2}), \
            mock.patch("iic_booking.impact.facility_mapping.verify_user",
                       return_value={"queued": 1, "queued_ids": [7]}) as verify, \
            mock.patch("iic_booking.impact.match_review.notify_reviewers", return_value=2) as notify:
        settings.PUBLICATION_MAPPING_ENABLED = False
        assert author_review.follow_up(w.faculty.pk)["check"].startswith("PUBLICATION_MAPPING_ENABLED")
        verify.assert_not_called()
        settings.PUBLICATION_MAPPING_ENABLED = True
        out = author_review.follow_up(w.faculty.pk)
    assert out == {"discovery": {"found": 3, "added": 2}, "queued": 1, "reviewers_notified": 2}
    notify.assert_called_once_with([7])


def test_researcher_confirms_or_rejects_their_automatic_link_but_admins_cannot(lookalikes, django_capture_on_commit_callbacks):
    w = lookalikes
    main_admin = make_user(user_type=UserType.ADMIN, name="Main Admin One")
    # An administrator leaves an automatic link alone.
    client_for(main_admin).post(f"{API}{w.faculty2.pk}/", {"reject": [w.b_auto.pk]}, format="json")
    w.b_auto.refresh_from_db()
    assert w.b_auto.status == AuthorLinkStatus.AUTO_LINKED
    # The researcher confirms it: no new follow-up (it was already being scanned).
    with mock.patch.object(author_review, "_start_follow_up") as follow:
        with django_capture_on_commit_callbacks(execute=True):
            r = client_for(w.faculty2).post(f"{API}{w.faculty2.pk}/", {"confirm": [w.b_auto.pk]}, format="json")
    assert r.status_code == 200 and r.json()["scan_started"] is False
    follow.assert_not_called()
    w.b_auto.refresh_from_db()
    assert w.b_auto.status == AuthorLinkStatus.CONFIRMED
    # ...or says it is not them.
    w.b_auto.status = AuthorLinkStatus.AUTO_LINKED
    w.b_auto.save()
    client_for(w.faculty2).post(f"{API}{w.faculty2.pk}/", {"reject": [w.b_auto.pk]}, format="json")
    w.b_auto.refresh_from_db()
    assert w.b_auto.status == AuthorLinkStatus.REJECTED


def test_faculty_with_no_records_gets_one_background_name_search(world, settings):
    from django.core.cache import cache

    w = world
    settings.INSTITUTE_ROR_ID = "https://ror.org/00582g326"
    cache.delete(author_review.SELF_SEARCH_CACHE.format(w.faculty.pk))
    with mock.patch("iic_booking.impact.tasks.impact_author_self_search_task.delay") as delay:
        first = client_for(w.faculty).get(API + "mine/").json()
        second = client_for(w.faculty).get(API + "mine/").json()
    assert first["searching"] is True and second["searching"] is False  # at most once a week
    delay.assert_called_once_with(w.faculty.pk)
    student = make_user(user_type=UserType.STUDENT, name="Student One")
    with mock.patch("iic_booking.impact.tasks.impact_author_self_search_task.delay") as delay:
        assert client_for(student).get(API + "mine/").json()["searching"] is False
    delay.assert_not_called()


def test_self_search_stores_the_name_matches(world):
    w = world
    found = [{"openalex_author_id": "Z1", "display_name": w.faculty.name, "orcid": "", "works_count": 5,
              "institutions": ["Indian Institute of Technology Roorkee"], "rationale": "name", "confidence": 0.7,
              "status": AuthorLinkStatus.NEEDS_CONFIRMATION}]
    with mock.patch("iic_booking.impact.author_linking.link_user", return_value=found) as link:
        out = author_review.self_search(w.faculty.pk)
    link.assert_called_once()
    assert out == {"candidates": 1, "auto_linked": 0}
