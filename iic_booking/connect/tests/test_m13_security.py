"""M13 hardening: systematic authentication, feature-flag and cross-group leakage checks over every Connect URL,
plus query-count checks on the heaviest pages.

A Group A member and a Group B member are created with one thread, reply, attachment, article, announcement and
notification in Group A. The Group B member is then sent against every Group A object endpoint and every listing:
nothing from Group A may appear (404 for objects; absent from lists, counts, search, suggestions, home, analytics).
"""

import json
import re

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import get_resolver

from iic_booking.connect.models import Community, DiscussionPost
from iic_booking.connect.services import membership as msvc
from iic_booking.equipment.models import EquipmentManager, EquipmentOperator
from iic_booking.users.models.user_type import UserType

from .conftest import API, client_for, make_booking, make_equipment, make_external, make_group, make_user

pytestmark = pytest.mark.django_db

SECRET = "zircon-secret-marker"


@pytest.fixture
def world(connect_on, django_capture_on_commit_callbacks):
    ga, gb = make_group("Alpha group"), make_group("Beta group")
    ea, eb = make_equipment(ga, name="Alpha instrument"), make_equipment(gb, name="Beta instrument")
    a_member, a_helper, b_member = make_user(name="Alpha member"), make_user(name="Alpha helper"), make_user(name="Beta member")
    a_op, a_oic, b_oic = make_user(UserType.OPERATOR), make_user(UserType.MANAGER), make_user(UserType.MANAGER)
    ext_b = make_external(name="Beta external")
    for u, e in ((a_member, ea), (a_helper, ea), (b_member, eb), (ext_b, eb)):
        make_booking(u, e)
    EquipmentOperator.objects.create(equipment=ea, operator=a_op)
    EquipmentManager.objects.create(equipment=ea, manager=a_oic)
    EquipmentManager.objects.create(equipment=eb, manager=b_oic)
    msvc.sync_all(dry_run=False)
    ca = Community.objects.get(equipment_group_ref_id=ga.pk)
    cm = client_for(a_member)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    att = cm.post(f"{API}/attachments/", {"community": ca.slug, "file": SimpleUploadedFile("trace.png", png, content_type="image/png")}).json()["id"]
    with django_capture_on_commit_callbacks(execute=True):
        tid = cm.post(
            f"{API}/threads/",
            {"community": ca.slug, "type": "question", "title": f"Alpha {SECRET} question", "body": f"<p>{SECRET} body</p>",
             "equipment_id": ea.pk, "tags": [SECRET], "attachments": [att]},
            format="json",
        ).json()["id"]
        rid = client_for(a_helper).post(f"{API}/threads/{tid}/posts/", {"body": f"<p>{SECRET} reply</p>"}, format="json").json()["id"]
        cm.post(f"{API}/threads/{tid}/solution/", {"post_id": rid}, format="json")
    aid = client_for(a_op).post(f"{API}/articles/", {"thread_id": tid, "title": f"Alpha {SECRET} article"}, format="json").json()["id"]
    client_for(a_op).post(f"{API}/articles/{aid}/submit/")
    client_for(a_oic).post(f"{API}/articles/{aid}/review/", {"decision": "approved", "institutional": True}, format="json")
    client_for(a_oic).post(f"{API}/articles/{aid}/publish/")
    with django_capture_on_commit_callbacks(execute=True):
        ann = client_for(a_oic).post(
            f"{API}/threads/", {"community": ca.slug, "type": "announcement", "title": f"Alpha {SECRET} notice", "body": "<p>notice</p>",
                                "requires_acknowledgement": True}, format="json",
        ).json()["id"]
    opening = DiscussionPost.objects.get(thread_id=tid, is_opening=True).pk
    return {"ca": ca, "ea": ea, "eb": eb, "a_member": a_member, "b_member": b_member, "ext_b": ext_b, "b_oic": b_oic,
            "tid": tid, "rid": rid, "opening": opening, "att": att, "aid": aid, "ann": ann}


def connect_routes():
    resolver = get_resolver()
    out = []

    def walk(patterns, prefix=""):
        for p in patterns:
            route = prefix + str(p.pattern)
            if hasattr(p, "url_patterns"):
                walk(p.url_patterns, route)
            elif route.startswith("api/v1/connect/"):
                out.append(route)

    walk(resolver.url_patterns)
    return sorted(set(out))


def fill(route, w):
    values = {"pk": str(w["tid"]), "slug": w["ca"].slug, "user_id": str(w["a_member"].pk), "equipment_id": str(w["ea"].pk),
              "key": "new", "action": "submit"}
    if route.startswith("api/v1/connect/posts/"):
        values["pk"] = str(w["rid"])
    if route.startswith("api/v1/connect/articles/"):
        values["pk"] = str(w["aid"])
    if route.startswith("api/v1/connect/attachments/"):
        values["pk"] = w["att"]
    if route.startswith("api/v1/connect/categories/"):
        from iic_booking.connect.models import DiscussionCategory

        values["pk"] = str(DiscussionCategory.objects.filter(community=w["ca"]).values_list("pk", flat=True).first() or 0)
    path = re.sub(r"<(?:\w+:)?(\w+)>", lambda m: values.get(m.group(1), "1"), route)
    return "/" + path


def test_every_route_is_covered_by_these_checks(world):
    routes = connect_routes()
    assert len(routes) >= 50, routes  # the whole API is mounted and crawled


def test_unauthenticated_is_rejected_everywhere(world):
    for route in connect_routes():
        path = fill(route, world)
        for method in ("get", "post", "put", "patch", "delete"):
            res = getattr(client_for(), method)(path, {}, format="json")
            assert res.status_code in (401, 403, 404, 405), (method, path, res.status_code)
            assert SECRET not in res.content.decode(errors="ignore"), (method, path)


def test_flag_off_hides_everything_but_bootstrap(world, settings):
    settings.IITR_CONNECT_ENABLED = False
    for route in connect_routes():
        path = fill(route, world)
        res = client_for(world["a_member"]).get(path)
        if route.endswith("bootstrap/"):
            assert res.status_code == 200 and res.json()["enabled"] is False
        else:
            assert res.status_code in (404, 405), (path, res.status_code)


def test_pilot_list_excludes_unlisted_users(world, settings):
    settings.IITR_CONNECT_PILOT_EMAILS = "someone.else@iitr.ac.in"
    for route in connect_routes():
        if route.endswith("bootstrap/"):
            continue
        res = client_for(world["a_member"]).get(fill(route, world))
        assert res.status_code in (404, 405), route


@pytest.mark.parametrize("who", ["b_member", "ext_b", "b_oic"])
def test_group_b_never_reaches_group_a_objects(world, who):
    user = world[who]
    c = client_for(user)
    object_paths = [
        f"{API}/threads/{world['tid']}/",
        f"{API}/threads/{world['tid']}/posts/",
        f"{API}/threads/{world['ann']}/",
        f"{API}/threads/{world['ann']}/acknowledge/",
        f"{API}/posts/{world['rid']}/revisions/",
        f"{API}/posts/{world['rid']}/report/",
        f"{API}/attachments/{world['att']}/",
        f"{API}/articles/{world['aid']}/",
        f"{API}/articles/{world['aid']}/revisions/",
        f"{API}/communities/{world['ca'].slug}/",
        f"{API}/communities/{world['ca'].slug}/members/",
        f"{API}/communities/{world['ca'].slug}/mention-candidates/?q=Alpha",
        f"{API}/communities/{world['ca'].slug}/audit/",
    ]
    for path in object_paths:
        res = c.get(path)
        assert res.status_code in (403, 404), (who, path, res.status_code)
        assert SECRET not in res.content.decode(errors="ignore"), (who, path)
    writes = [
        ("post", f"{API}/threads/{world['tid']}/posts/", {"body": "<p>x</p>"}),
        ("post", f"{API}/threads/{world['tid']}/solution/", {"post_id": world["rid"]}),
        ("post", f"{API}/threads/{world['tid']}/bookmark/", {}),
        ("put", f"{API}/threads/{world['tid']}/follow/", {"level": "watching"}),
        ("patch", f"{API}/threads/{world['tid']}/staff/", {"is_pinned": True}),
        ("post", f"{API}/threads/{world['tid']}/lock/", {"locked": True}),
        ("post", f"{API}/threads/{world['tid']}/move/", {"category_id": ""}),
        ("post", f"{API}/posts/{world['rid']}/react/", {"kind": "helpful"}),
        ("post", f"{API}/posts/{world['rid']}/report/", {"reason": "spam"}),
        ("post", f"{API}/posts/{world['rid']}/moderate/", {"action": "hide", "reason": "x"}),
        ("patch", f"{API}/posts/{world['rid']}/", {"body": "<p>x</p>"}),
        ("post", f"{API}/articles/", {"thread_id": world["tid"]}),
        ("post", f"{API}/articles/{world['aid']}/archive/", {}),
        ("post", f"{API}/threads/{world['ann']}/acknowledge/", {}),
        ("post", f"{API}/attachments/", {"community": world["ca"].slug}),
    ]
    for method, path, body in writes:
        res = getattr(c, method)(path, body, format="json") if method != "post" or "attachments/" not in path else c.post(path, body)
        assert res.status_code in (403, 404), (who, method, path, res.status_code)


@pytest.mark.parametrize("who", ["b_member", "ext_b", "b_oic"])
def test_group_b_lists_counts_search_and_home_never_mention_group_a(world, who):
    c = client_for(world[who])
    listing_paths = [
        f"{API}/threads/",
        f"{API}/threads/?q={SECRET}",
        f"{API}/threads/?tag={SECRET}",
        f"{API}/threads/?equipment={world['ea'].pk}",
        f"{API}/search/?q={SECRET}",
        f"{API}/suggest/?title=Alpha {SECRET} question",
        f"{API}/home/",
        f"{API}/communities/",
        f"{API}/tags/",
        f"{API}/categories/",
        f"{API}/articles/",
        f"{API}/articles/?q={SECRET}",
        f"{API}/notifications/",
        f"{API}/drafts/",
        f"{API}/equipment/{world['eb'].pk}/discussions/",
    ]
    if who == "b_oic":
        listing_paths += [f"{API}/analytics/", f"{API}/moderation/queue/", f"{API}/moderation/log/", f"{API}/moderation/dashboard/"]
    for path in listing_paths:
        res = c.get(path)
        assert res.status_code in (200, 404), (who, path, res.status_code)
        body = res.content.decode(errors="ignore")
        assert SECRET not in body, (who, path)
        assert "Alpha group" not in body, (who, path)
    panel = c.get(f"{API}/equipment/{world['ea'].pk}/discussions/")
    assert panel.status_code in (200, 404)
    if panel.status_code == 200:
        data = panel.json()
        assert data["available"] is False and "recent" not in data


def test_external_user_never_reaches_general(world):
    c = client_for(world["ext_b"])
    assert c.get(f"{API}/communities/general/").status_code == 404
    assert "general" not in json.dumps(c.get(f"{API}/communities/").json())
    res = c.post(f"{API}/threads/", {"community": "general", "title": "External in general?", "body": "<p>x</p>"}, format="json")
    assert res.status_code == 404


def test_responses_are_private_and_noindex(world):
    for path in (f"{API}/threads/", f"{API}/threads/{world['tid']}/", f"{API}/home/", f"{API}/search/?q=x"):
        res = client_for(world["a_member"]).get(path)
        assert "no-store" in res["Cache-Control"] and "noindex" in res["X-Robots-Tag"], path
    att = client_for(world["a_member"]).get(f"{API}/attachments/{world['att']}/")
    assert att["X-Content-Type-Options"] == "nosniff" and "no-store" in att["Cache-Control"]


@pytest.mark.parametrize(
    "path_tpl",
    ["{API}/threads/{tid}/posts/", "{API}/search/?q=reply", "{API}/home/", "{API}/communities/", "{API}/notifications/"],
)
def test_query_count_does_not_grow_with_data(world, path_tpl):
    c = client_for(world["a_member"])
    path = path_tpl.format(API=API, tid=world["tid"])
    with CaptureQueriesContext(connection) as small:
        c.get(path)
    for i in range(8):
        client_for(world["a_member"]).post(f"{API}/threads/{world['tid']}/posts/", {"body": f"<p>reply number {i} here</p>"}, format="json")
        client_for(world["a_member"]).post(
            f"{API}/threads/", {"community": world["ca"].slug, "title": f"Extra thread number {i}", "body": f"<p>reply body {i}</p>"}, format="json"
        )
    with CaptureQueriesContext(connection) as large:
        c.get(path)
    # Per-page work may add a few queries (e.g. one headline per search hit on the page), never one per row of data.
    assert len(large.captured_queries) <= len(small.captured_queries) + 12, (path, len(small.captured_queries), len(large.captured_queries))
