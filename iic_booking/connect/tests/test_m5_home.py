"""M5 backend support: landing page sections and @mention suggestions never leak other groups."""

import pytest

from iic_booking.connect.models import Community
from iic_booking.connect.models import DiscussionThread
from iic_booking.connect.services import membership as msvc

from .conftest import API
from .conftest import client_for
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_external
from .conftest import make_group
from .conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def two(connect_on):
    ga, gb = make_group("Alpha"), make_group("Beta")
    ea, eb = make_equipment(ga), make_equipment(gb)
    a1 = make_user(name="Asha Verma")
    a2 = make_user(name="Arun Kumar")
    b1 = make_user(name="Asha Banerjee")
    ext = make_external(name="Ashok Industry")
    for u, e in ((a1, ea), (a2, ea), (b1, eb), (ext, ea)):
        make_booking(u, e)
    msvc.sync_all(dry_run=False)
    return {
        "ca": Community.objects.get(equipment_group_ref_id=ga.pk),
        "cb": Community.objects.get(equipment_group_ref_id=gb.pk),
        "a1": a1, "a2": a2, "b1": b1, "ext": ext,
    }


def post(user, community, title, **extra):
    data = {"community": community.slug, "title": title, "body": f"<p>{title} body</p>", **extra}
    res = client_for(user).post(f"{API}/threads/", data, format="json")
    assert res.status_code == 201, res.content
    return res.json()["id"]


def all_titles(payload):
    return {t["title"] for key in ("latest", "trending", "recently_solved", "technical_support", "announcements") for t in payload[key]}


def test_home_sections_only_readable(two):
    post(two["a1"], two["ca"], "Alpha detector question", type="question")
    secret = post(two["b1"], two["cb"], "Beta confidential issue", type="technical_issue")
    DiscussionThread.objects.filter(pk=secret).update(reply_count=50)  # would top "trending" if it leaked
    res = client_for(two["a2"]).get(f"{API}/home/")
    assert res.status_code == 200
    titles = all_titles(res.json())
    assert "Alpha detector question" in titles and "Beta confidential issue" not in titles
    assert "Alpha detector question" in {t["title"] for t in res.json()["technical_support"]}


def test_active_contributors_rank_by_help_not_volume(two):
    tid = post(two["a1"], two["ca"], "Alpha thread for answers")
    c2 = client_for(two["a2"])
    reply = c2.post(f"{API}/threads/{tid}/posts/", {"body": "<p>Use the standard.</p>"}, format="json").json()
    client_for(two["a1"]).post(f"{API}/posts/{reply['id']}/react/", {"kind": "helpful"}, format="json")
    for i in range(3):
        client_for(two["a1"]).post(f"{API}/threads/{tid}/posts/", {"body": f"<p>chatter {i}</p>"}, format="json")
    people = client_for(two["a2"]).get(f"{API}/home/").json()["active_contributors"]
    assert people[0]["id"] == two["a2"].pk and people[0]["helpful_received"] == 1
    # Group B activity never shows up for Group A readers
    tb = post(two["b1"], two["cb"], "Beta thread for answers")
    for i in range(5):
        client_for(two["b1"]).post(f"{API}/threads/{tb}/posts/", {"body": f"<p>beta {i}</p>"}, format="json")
    ids = {p["id"] for p in client_for(two["a2"]).get(f"{API}/home/").json()["active_contributors"]}
    assert two["b1"].pk not in ids


def test_mention_candidates_limited_to_readers(two):
    res = client_for(two["a1"]).get(f"{API}/communities/{two['ca'].slug}/mention-candidates/?q=Ash")
    ids = {r["id"] for r in res.json()["results"]}
    assert two["ext"].pk in ids and two["b1"].pk not in ids and two["a1"].pk not in ids
    general = client_for(two["a1"]).get(f"{API}/communities/general/mention-candidates/?q=Ash").json()["results"]
    gids = {r["id"] for r in general}
    assert two["b1"].pk in gids and two["ext"].pk not in gids  # externals never offered in General
    assert client_for(two["a1"]).get(f"{API}/communities/{two['cb'].slug}/mention-candidates/?q=Ash").status_code == 404


def test_home_requires_flag_and_auth(settings):
    settings.IITR_CONNECT_ENABLED = False
    assert client_for(make_user()).get(f"{API}/home/").status_code == 404
    settings.IITR_CONNECT_ENABLED = True
    assert client_for().get(f"{API}/home/").status_code in (401, 403)


def test_solved_sort_orders_by_solved_time(two):
    from django.utils import timezone

    first = post(two["a1"], two["ca"], "Solved first question", type="question")
    second = post(two["a1"], two["ca"], "Solved second question", type="question")
    DiscussionThread.objects.filter(pk=first).update(solved_at=timezone.now() - timezone.timedelta(days=1))
    DiscussionThread.objects.filter(pk=second).update(solved_at=timezone.now())
    ids = [t["id"] for t in client_for(two["a2"]).get(f"{API}/threads/?status=solved&sort=solved").json()["results"]]
    assert ids == [second, first]
