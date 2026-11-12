"""M6: permission-aware search and "find a solution before raising a query"."""

import pytest
from django.utils import timezone

from iic_booking.connect.models import Community
from iic_booking.connect.models import ConnectSearchLog
from iic_booking.connect.models import DiscussionPost
from iic_booking.connect.models import DiscussionThread
from iic_booking.connect.models import Visibility
from iic_booking.connect.services import membership as msvc
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import client_for
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_group
from .conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def s(connect_on):
    ga, gb = make_group("Spectroscopy"), make_group("Microscopy")
    ea = make_equipment(ga, name="Raman Spectrometer")
    eb = make_equipment(gb, name="Confocal Microscope")
    a, a2, b = make_user(name="Asha Verma"), make_user(name="Arun Kumar"), make_user(name="Bela Roy")
    mod = make_user(UserType.MANAGER, name="Dr Kavita Rao")
    for u, e in ((a, ea), (a2, ea), (b, eb)):
        make_booking(u, e)
    from iic_booking.equipment.models import EquipmentManager

    EquipmentManager.objects.create(equipment=ea, manager=mod)
    msvc.sync_all(dry_run=False)
    return {
        "ca": Community.objects.get(equipment_group_ref_id=ga.pk), "cb": Community.objects.get(equipment_group_ref_id=gb.pk),
        "ea": ea, "eb": eb, "a": a, "a2": a2, "b": b, "mod": mod,
    }


def post(user, community, title, body, **extra):
    res = client_for(user).post(f"{API}/threads/", {"community": community.slug, "title": title, "body": body, **extra}, format="json")
    assert res.status_code == 201, res.content
    return res.json()["id"]


def reply(user, tid, body):
    res = client_for(user).post(f"{API}/threads/{tid}/posts/", {"body": body}, format="json")
    assert res.status_code == 201, res.content
    return res.json()["id"]


def search(user, q, **params):
    res = client_for(user).get(f"{API}/search/", {"q": q, **params})
    assert res.status_code == 200, res.content
    return res.json()


def ids(payload):
    return [r["id"] for r in payload["results"]]


# --- Global search -------------------------------------------------------------------------------------------------


def test_matches_title_body_reply_tag_equipment_and_author(s):
    t1 = post(s["a"], s["ca"], "Fluorescence background in Raman spectra", "<p>Strong baseline at 785 nm.</p>", tags=["baseline"])
    t2 = post(s["a2"], s["ca"], "Laser alignment routine", "<p>How often should alignment run?</p>", equipment_id=s["ea"].pk)
    reply(s["mod"], t2, "<p>Use the silicon wafer peak at 520 wavenumbers for calibration.</p>")
    assert t1 in ids(search(s["a"], "fluorescence"))
    assert t1 in ids(search(s["a"], "785"))
    assert t2 in ids(search(s["a"], "silicon wafer"))  # reply text
    assert t1 in ids(search(s["a"], "baseline"))  # tag / body
    assert t2 in ids(search(s["a"], "Raman Spectrometer"))  # equipment name
    assert t2 in ids(search(s["a"], "Arun"))  # author
    hit = next(r for r in search(s["a"], "silicon wafer")["results"] if r["id"] == t2)
    assert hit["matched_in"] == "reply" and "<mark>" in hit["snippet_html"]


def test_other_group_content_never_matches(s):
    post(s["b"], s["cb"], "Confocal pinhole secret settings", "<p>Pinhole 1 AU and the lab door code.</p>")
    for q in ("pinhole", "Confocal", "Bela", "door code"):
        assert search(s["a"], q)["count"] == 0, q
    res = client_for(s["a"]).get(f"{API}/suggest/", {"title": "Confocal pinhole settings question"})
    assert res.json()["results"] == []


def test_hidden_or_deleted_reply_text_is_not_searchable(s):
    tid = post(s["a"], s["ca"], "Detector cooling question", "<p>How long to cool?</p>")
    pid = reply(s["a2"], tid, "<p>Use the secret bypass valve marked zeta.</p>")
    assert tid in ids(search(s["a"], "zeta"))
    DiscussionPost.objects.filter(pk=pid).update(visibility=Visibility.HIDDEN)
    assert search(s["a"], "zeta")["count"] == 0
    # Search only ever matches visible text, for moderators too; hidden posts are handled in the moderation queue.
    assert search(s["mod"], "zeta")["count"] == 0
    DiscussionPost.objects.filter(pk=pid).update(visibility=Visibility.DELETED)
    assert search(s["a"], "zeta")["count"] == 0


def test_removed_thread_not_searchable(s):
    tid = post(s["a"], s["ca"], "Removed thread about omega", "<p>omega omega</p>")
    DiscussionThread.objects.filter(pk=tid).update(visibility=Visibility.REMOVED)
    assert search(s["a2"], "omega")["count"] == 0


def test_filters_and_sorts_apply_to_search(s):
    q1 = post(s["a"], s["ca"], "Raman peak shift question", "<p>peak shift</p>", type="question")
    post(s["a"], s["ca"], "Raman peak shift idea", "<p>peak shift suggestion</p>", type="suggestion")
    assert ids(search(s["a"], "peak shift", type="question")) == [q1]
    DiscussionThread.objects.filter(pk=q1).update(solved_at=timezone.now())
    assert ids(search(s["a"], "peak shift", status="solved")) == [q1]
    assert search(s["a"], "peak shift", sort="latest")["count"] == 2
    assert search(s["a"], "peak shift")["facets"]["types"] == {"question": 1, "suggestion": 1}


def test_snippet_never_carries_markup_from_user_text(s):
    tid = post(s["a"], s["ca"], "Escaping check thread", "<p>calibrate &lt;img src=x onerror=alert(1)&gt; now</p>")
    hit = next(r for r in search(s["a"], "calibrate")["results"] if r["id"] == tid)
    assert "<img" not in hit["snippet_html"] and "&lt;img" in hit["snippet_html"]
    assert "<mark>" in hit["snippet_html"]


def test_search_logged_for_analytics(s):
    search(s["a"], "  Raman   Baseline ")
    row = ConnectSearchLog.objects.latest("created_at")
    assert row.query == "raman baseline" and row.kind == "search"


def test_empty_query_and_auth(s, settings):
    assert search(s["a"], "")["count"] == 0
    assert client_for().get(f"{API}/search/", {"q": "x"}).status_code in (401, 403)
    settings.IITR_CONNECT_ENABLED = False
    assert client_for(s["a"]).get(f"{API}/search/", {"q": "x"}).status_code == 404


# --- Find a solution before raising a query ----------------------------------------------------------------------------


def test_suggest_prefers_solved_and_same_equipment_with_accepted_preview(s):
    plain = post(s["a2"], s["ca"], "Raman baseline correction method", "<p>Which polynomial order?</p>")
    solved = post(s["a2"], s["ca"], "Raman baseline correction on the spectrometer", "<p>Fluorescence baseline</p>", equipment_id=s["ea"].pk)
    answer = reply(s["mod"], solved, "<p>Use asymmetric least squares with lambda 1e5.</p>")
    DiscussionThread.objects.filter(pk=solved).update(solved_at=timezone.now(), accepted_post_id=answer)
    res = client_for(s["a"]).get(f"{API}/suggest/", {"title": "Baseline correction for Raman spectra", "community": s["ca"].slug, "equipment": s["ea"].pk})
    results = res.json()["results"]
    assert [r["id"] for r in results][:2] == [solved, plain]
    preview = results[0]["accepted_answer"]
    assert "asymmetric least squares" in preview["excerpt"] and preview["author"] == "Dr Kavita Rao"
    assert results[0]["is_solved"] and results[0]["equipment"]["id"] == s["ea"].pk


def test_suggest_needs_meaningful_words_and_never_blocks(s):
    assert client_for(s["a"]).get(f"{API}/suggest/", {"title": "how to the"}).json()["results"] == []
    post(s["a"], s["ca"], "Raman baseline correction method", "<p>x</p>")
    assert post(s["a2"], s["ca"], "Raman baseline correction method again", "<p>different body</p>")  # posting still works


def test_accepted_preview_hidden_when_answer_not_readable(s):
    tid = post(s["a2"], s["ca"], "Raman laser power question", "<p>What power?</p>")
    answer = reply(s["mod"], tid, "<p>Use 10 mW.</p>")
    DiscussionThread.objects.filter(pk=tid).update(solved_at=timezone.now(), accepted_post_id=answer)
    DiscussionPost.objects.filter(pk=answer).update(visibility=Visibility.HIDDEN)
    results = client_for(s["a"]).get(f"{API}/suggest/", {"title": "Raman laser power setting"}).json()["results"]
    assert results and results[0]["accepted_answer"] is None
