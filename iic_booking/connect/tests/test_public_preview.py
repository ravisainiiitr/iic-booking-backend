import pytest

from iic_booking.connect.models import Visibility

from .conftest import API
from .conftest import add_member
from .conftest import client_for
from .conftest import general_community
from .conftest import group_community
from .conftest import make_thread
from .conftest import make_user

pytestmark = pytest.mark.django_db

URL = f"{API}/public/"


def test_off_when_module_off(settings):
    settings.IITR_CONNECT_ENABLED = False
    assert client_for().get(URL).status_code == 404


def test_off_during_pilot(connect_on):
    connect_on.IITR_CONNECT_PILOT_EMAILS = "someone@iitr.ac.in"
    assert client_for().get(URL).status_code == 404


def test_visitor_sees_directory_and_general_titles_only(connect_on):
    author = make_user()
    general = general_community()
    group = group_community()
    hidden_group = group_community(is_test_only=True)
    add_member(group, author)
    make_thread(general, author, title="Where is the shared plotter?", body="<p>secret body text</p>")
    make_thread(general, author, title="Hidden general thread", visibility=Visibility.HIDDEN)
    make_thread(group, author, title="Group-only question")

    res = client_for().get(URL)
    assert res.status_code == 200
    data = res.json()
    assert data["enabled"] is True

    slugs = [c["slug"] for c in data["communities"]]
    assert slugs[0] == general.slug
    assert group.slug in slugs
    assert hidden_group.slug not in slugs
    group_row = next(c for c in data["communities"] if c["slug"] == group.slug)
    assert group_row["member_count"] == 1
    assert group_row["thread_count"] == 1

    titles = [t["title"] for t in data["general_threads"]]
    assert titles == ["Where is the shared plotter?"]
    body = res.content.decode()
    assert "Group-only question" not in body
    assert "secret body text" not in body
    assert author.email not in body
    assert "author" not in data["general_threads"][0]


def test_thread_detail_still_needs_sign_in(connect_on):
    author = make_user()
    thread = make_thread(general_community(), author)
    assert client_for().get(f"{API}/threads/{thread.pk}/").status_code == 401
    assert client_for().post(f"{API}/threads/", {"title": "x"}, format="json").status_code == 401
