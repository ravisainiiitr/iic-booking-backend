"""M4: threads, replies, edits, deletes, reactions, follows, bookmarks, polls, drafts, tags, categories, attachments."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from iic_booking.connect.models import CommunityMembership
from iic_booking.connect.models import ConnectSettings
from iic_booking.connect.models import DiscussionAttachment
from iic_booking.connect.models import DiscussionCategory
from iic_booking.connect.models import DiscussionPost
from iic_booking.connect.models import DiscussionPostRevision
from iic_booking.connect.models import DiscussionThread
from iic_booking.connect.models import MembershipStatus
from iic_booking.connect.models import ModerationAction
from iic_booking.connect.models import Visibility
from iic_booking.connect.services import membership as msvc
from iic_booking.connect.services import threads as tsvc
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import client_for
from .conftest import general_community
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_external
from .conftest import make_group
from .conftest import make_user

pytestmark = pytest.mark.django_db

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7V\xbd\xfa\x00\x00\x00\x00IEND\xaeB`\x82"
)


class World:
    pass


@pytest.fixture
def w(connect_on):
    """Two groups: A (member, operator, OIC, equipment) and B (another member); plus an outsider and an external."""
    x = World()
    x.ga, x.gb = make_group("Group A"), make_group("Group B")
    x.eqa, x.eqb = make_equipment(x.ga), make_equipment(x.gb)
    x.member, x.member2, x.b_member = make_user(), make_user(), make_user()
    x.op, x.oic = make_user(UserType.OPERATOR), make_user(UserType.MANAGER)
    x.external = make_external()
    make_booking(x.member, x.eqa)
    make_booking(x.member2, x.eqa)
    make_booking(x.external, x.eqa)
    make_booking(x.b_member, x.eqb)
    EquipmentOperator.objects.create(equipment=x.eqa, operator=x.op)
    EquipmentManager.objects.create(equipment=x.eqa, manager=x.oic)
    msvc.sync_all(dry_run=False)
    from iic_booking.connect.models import Community

    x.ca = Community.objects.get(equipment_group_ref_id=x.ga.pk)
    x.cb = Community.objects.get(equipment_group_ref_id=x.gb.pk)
    x.general = general_community()
    return x


def new_thread(user, community, **extra):
    data = {"community": community.slug, "title": "Baseline drift on the detector", "body": "<p>Seeing drift after warm-up.</p>"}
    data.update(extra)
    return client_for(user).post(f"{API}/threads/", data, format="json")


# --- Create ---------------------------------------------------------------------------------------------------------


def test_member_creates_thread_with_tags_and_equipment(w):
    res = new_thread(w.member, w.ca, type="question", tags=["Calibration", "detector"], equipment_id=w.eqa.pk)
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["type"] == "question" and body["equipment"]["id"] == w.eqa.pk
    assert {t["slug"] for t in body["tags"]} == {"calibration", "detector"}
    assert body["permissions"]["can_reply"] and body["opening_post"]["body_html"].startswith("<p>")
    t = DiscussionThread.objects.get(pk=body["id"])
    assert t.search_vector is not None and t.equipment_ref_id == w.eqa.pk
    assert t.subscriptions.filter(user=w.member).exists()  # auto-follow own thread


def test_body_is_sanitised(w):
    res = new_thread(w.member, w.ca, body='<p onclick="x()">ok</p><script>alert(1)</script><img src=x onerror=alert(1)>')
    html = res.json()["opening_post"]["body_html"]
    assert "script" not in html and "onclick" not in html and "onerror" not in html


def test_cannot_post_in_other_group_or_general_as_external(w):
    assert new_thread(w.member, w.cb).status_code == 404
    assert new_thread(w.external, w.general).status_code == 404
    assert new_thread(w.external, w.ca).status_code == 201  # externals take part in their group


def test_suspended_member_and_archived_community_cannot_post(w):
    CommunityMembership.objects.filter(community=w.ca, user=w.member).update(status=MembershipStatus.SUSPENDED)
    assert new_thread(w.member, w.ca).status_code == 403
    w.ca.status = "archived"
    w.ca.save()
    assert new_thread(w.member2, w.ca).status_code == 403


def test_announcements_and_staff_only_categories_need_staff(w):
    assert new_thread(w.member, w.ca, type="announcement").status_code == 403
    assert new_thread(w.op, w.ca, type="announcement").status_code == 201
    ann = DiscussionCategory.objects.get(community=w.general, slug="announcements")
    assert new_thread(w.member, w.general, category_id=ann.pk).status_code == 403
    portal = DiscussionCategory.objects.get(community=w.general, slug="portal-support")
    assert new_thread(w.member, w.general, category_id=portal.pk).status_code == 201


def test_equipment_must_belong_to_the_group(w):
    res = new_thread(w.member, w.ca, equipment_id=w.eqb.pk)
    assert res.status_code == 400 and res.json()["field"] == "equipment"
    hidden = make_equipment(w.ga, visible_to_test_accounts_only=True)
    assert new_thread(w.member, w.ca, equipment_id=hidden.pk).status_code == 400


def test_category_of_other_community_rejected(w):
    portal = DiscussionCategory.objects.get(community=w.general, slug="portal-support")
    assert new_thread(w.member, w.ca, category_id=portal.pk).status_code == 400


def test_validation_and_duplicate_protection(w):
    assert new_thread(w.member, w.ca, title="short").status_code == 400
    assert new_thread(w.member, w.ca, body="   ").status_code == 400
    assert new_thread(w.member, w.ca).status_code == 201
    res = new_thread(w.member, w.ca, title="Same body again different title")
    assert res.status_code == 400 and "same text" in res.json()["detail"]


# --- Lists and detail ---------------------------------------------------------------------------------------------------


def test_feed_never_shows_other_group(w):
    new_thread(w.b_member, w.cb, title="Group B private matter")
    new_thread(w.member, w.ca, title="Group A open question")
    titles = {t["title"] for t in client_for(w.member).get(f"{API}/threads/").json()["results"]}
    assert "Group A open question" in titles and "Group B private matter" not in titles
    titles = {t["title"] for t in client_for(w.member).get(f"{API}/threads/?q=private").json()["results"]}
    assert "Group B private matter" not in titles
    titles = {t["title"] for t in client_for(w.external).get(f"{API}/threads/").json()["results"]}
    assert "Group B private matter" not in titles


def test_feed_filters_and_sorts(w):
    a = new_thread(w.member, w.ca, title="Question about equipment A", type="question", equipment_id=w.eqa.pk).json()["id"]
    new_thread(w.member2, w.ca, title="General chat about the lab", body="<p>hello lab</p>")
    c = client_for(w.member)
    assert [t["id"] for t in c.get(f"{API}/threads/?equipment={w.eqa.pk}").json()["results"]] == [a]
    assert [t["id"] for t in c.get(f"{API}/threads/?type=question").json()["results"]] == [a]
    assert {t["id"] for t in c.get(f"{API}/threads/?status=unanswered").json()["results"]} >= {a}
    assert [t["id"] for t in c.get(f"{API}/threads/?status=unsolved").json()["results"]] == [a]
    for sort in ("latest", "new", "discussed", "viewed", "helpful", "unanswered"):
        assert c.get(f"{API}/threads/?sort={sort}").status_code == 200


def test_cross_group_detail_posts_and_actions_are_404(w):
    tid = new_thread(w.b_member, w.cb).json()["id"]
    c = client_for(w.member)
    for method, path in (
        ("get", f"threads/{tid}/"),
        ("get", f"threads/{tid}/posts/"),
        ("post", f"threads/{tid}/posts/"),
        ("post", f"threads/{tid}/bookmark/"),
        ("put", f"threads/{tid}/follow/"),
        ("patch", f"threads/{tid}/"),
        ("delete", f"threads/{tid}/"),
    ):
        assert getattr(c, method)(f"{API}/{path}", {"body": "x", "level": "watching"}, format="json").status_code == 404, path
    opening = DiscussionPost.objects.get(thread_id=tid, is_opening=True)
    for method, path in (("patch", f"posts/{opening.pk}/"), ("post", f"posts/{opening.pk}/react/"),
                         ("get", f"posts/{opening.pk}/revisions/")):
        assert getattr(c, method)(f"{API}/{path}", {"kind": "helpful"}, format="json").status_code == 404, path


def test_unauthenticated_rejected_everywhere(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    for path in ("threads/", f"threads/{tid}/", f"threads/{tid}/posts/", "tags/", "categories/", "drafts/"):
        assert client_for().get(f"{API}/{path}").status_code in (401, 403), path


def test_view_count_dedupes(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    c = client_for(w.member2)
    c.get(f"{API}/threads/{tid}/")
    c.get(f"{API}/threads/{tid}/")
    assert DiscussionThread.objects.get(pk=tid).view_count == 1


def test_feed_query_count_does_not_grow_with_threads(w):
    for i in range(3):
        new_thread(w.member, w.ca, title=f"Thread number {i} here", body=f"<p>body {i}</p>", tags=["xrd"])
    c = client_for(w.member2)
    with CaptureQueriesContext(connection) as small:
        c.get(f"{API}/threads/")
    for i in range(3, 12):
        new_thread(w.member, w.ca, title=f"Thread number {i} here", body=f"<p>body {i}</p>", tags=["xrd"])
    with CaptureQueriesContext(connection) as large:
        c.get(f"{API}/threads/")
    assert len(large.captured_queries) <= len(small.captured_queries) + 1


# --- Replies -----------------------------------------------------------------------------------------------------------


def test_reply_and_reply_to_post(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    c2 = client_for(w.member2)
    r1 = c2.post(f"{API}/threads/{tid}/posts/", {"body": "<p>Try a longer warm-up.</p>"}, format="json")
    assert r1.status_code == 201
    r2 = client_for(w.op).post(f"{API}/threads/{tid}/posts/", {"body": "<p>Confirmed.</p>", "parent_id": r1.json()["id"]}, format="json")
    assert r2.status_code == 201 and r2.json()["parent_id"] == r1.json()["id"]
    assert r2.json()["author"]["badge"] == "Lab Operator (Lab In-charge)" and r2.json()["is_verified_response"]
    t = DiscussionThread.objects.get(pk=tid)
    assert t.reply_count == 2 and t.participant_count == 3
    posts = client_for(w.member).get(f"{API}/threads/{tid}/posts/").json()["results"]
    assert [p["id"] for p in posts] == [r1.json()["id"], r2.json()["id"]]


def test_reply_parent_from_other_thread_rejected(w):
    t1 = new_thread(w.member, w.ca).json()["id"]
    t2 = new_thread(w.member2, w.ca, title="Another thread entirely", body="<p>other</p>").json()["id"]
    other_post = DiscussionPost.objects.get(thread_id=t2, is_opening=True)
    res = client_for(w.member).post(f"{API}/threads/{t1}/posts/", {"body": "<p>hi</p>", "parent_id": other_post.pk}, format="json")
    assert res.status_code == 400


def test_locked_thread_only_moderators_reply(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    DiscussionThread.objects.filter(pk=tid).update(status="locked")
    assert client_for(w.member2).post(f"{API}/threads/{tid}/posts/", {"body": "<p>x</p>"}, format="json").status_code == 403
    assert client_for(w.oic).post(f"{API}/threads/{tid}/posts/", {"body": "<p>Locked: see the SOP.</p>"}, format="json").status_code == 201


# --- Edit / delete ----------------------------------------------------------------------------------------------------


def test_author_edit_creates_revision_and_window_applies(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    c = client_for(w.member)
    res = c.patch(f"{API}/threads/{tid}/", {"title": "Baseline drift after warm-up", "body": "<p>Updated details.</p>"}, format="json")
    assert res.status_code == 200 and res.json()["title"] == "Baseline drift after warm-up"
    opening = DiscussionPost.objects.get(thread_id=tid, is_opening=True)
    rev = DiscussionPostRevision.objects.get(post=opening)
    assert rev.title == "Baseline drift on the detector" and "Seeing drift" in rev.body_html
    assert c.get(f"{API}/posts/{opening.pk}/revisions/").status_code == 200
    assert client_for(w.member2).get(f"{API}/posts/{opening.pk}/revisions/").status_code == 404
    s = ConnectSettings.load()
    s.edit_window_minutes = 1
    s.save()
    DiscussionPost.objects.filter(pk=opening.pk).update(created_at=timezone.now() - timedelta(minutes=5))
    assert c.patch(f"{API}/threads/{tid}/", {"body": "<p>Too late</p>"}, format="json").status_code == 403


def test_moderator_edit_is_audited(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    res = client_for(w.oic).patch(f"{API}/threads/{tid}/", {"body": "<p>Removed a phone number.</p>", "reason": "Personal data"}, format="json")
    assert res.status_code == 200
    assert ModerationAction.objects.filter(action="edit", actor=w.oic, reason="Personal data").exists()
    assert DiscussionPostRevision.objects.filter(by_moderator=True, editor=w.oic).exists()


def test_other_member_cannot_edit(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    assert client_for(w.member2).patch(f"{API}/threads/{tid}/", {"body": "<p>x</p>"}, format="json").status_code == 403


def test_delete_rules(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    reply = client_for(w.member2).post(f"{API}/threads/{tid}/posts/", {"body": "<p>An answer.</p>"}, format="json").json()
    assert client_for(w.member).delete(f"{API}/threads/{tid}/").status_code == 403  # has replies
    assert client_for(w.member2).delete(f"{API}/posts/{reply['id']}/").status_code == 204
    assert DiscussionThread.objects.get(pk=tid).reply_count == 0
    assert client_for(w.member).delete(f"{API}/threads/{tid}/").status_code == 204
    assert DiscussionThread.objects.get(pk=tid).visibility == Visibility.DELETED
    assert client_for(w.member2).get(f"{API}/threads/{tid}/").status_code == 404
    assert ModerationAction.objects.filter(action="author_delete").count() == 2


def test_member_cannot_delete_someone_elses_reply(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    reply = client_for(w.member2).post(f"{API}/threads/{tid}/posts/", {"body": "<p>An answer.</p>"}, format="json").json()
    assert client_for(w.member).delete(f"{API}/posts/{reply['id']}/").status_code == 403


# --- Reactions, bookmarks, follows -----------------------------------------------------------------------------------


def test_reactions(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    opening = DiscussionPost.objects.get(thread_id=tid, is_opening=True)
    assert client_for(w.member).post(f"{API}/posts/{opening.pk}/react/", {"kind": "helpful"}, format="json").status_code == 403
    res = client_for(w.member2).post(f"{API}/posts/{opening.pk}/react/", {"kind": "helpful"}, format="json")
    assert res.json() == {"kind": "helpful", "active": True, "counts": {"helpful": 1}}
    assert DiscussionThread.objects.get(pk=tid).helpful_count == 1
    res = client_for(w.member2).post(f"{API}/posts/{opening.pk}/react/", {"kind": "helpful"}, format="json")
    assert res.json()["active"] is False and DiscussionThread.objects.get(pk=tid).helpful_count == 0
    assert client_for(w.member2).post(f"{API}/posts/{opening.pk}/react/", {"kind": "love"}, format="json").status_code == 400
    CommunityMembership.objects.filter(community=w.ca, user=w.member2).update(status=MembershipStatus.SUSPENDED)
    assert client_for(w.member2).post(f"{API}/posts/{opening.pk}/react/", {"kind": "thanks"}, format="json").status_code == 403


def test_bookmark_and_follow(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    c = client_for(w.member2)
    assert c.post(f"{API}/threads/{tid}/bookmark/").json() == {"bookmarked": True}
    assert [t["id"] for t in c.get(f"{API}/threads/?bookmarked=1").json()["results"]] == [tid]
    assert c.post(f"{API}/threads/{tid}/bookmark/").json() == {"bookmarked": False}
    assert c.put(f"{API}/threads/{tid}/follow/", {"level": "watching"}, format="json").json() == {"level": "watching"}
    assert [t["id"] for t in c.get(f"{API}/threads/?following=1").json()["results"]] == [tid]
    assert c.put(f"{API}/threads/{tid}/follow/", {"level": None}, format="json").json() == {"level": None}
    assert c.put(f"{API}/communities/{w.ca.slug}/follow/", {"level": "watching"}, format="json").status_code == 200
    assert c.put(f"{API}/communities/{w.cb.slug}/follow/", {"level": "watching"}, format="json").status_code == 404


# --- Polls ---------------------------------------------------------------------------------------------------------------


def test_poll(w):
    res = new_thread(w.member, w.ca, type="poll", title="Preferred training slot", poll_options=["Morning", "Evening", "Morning"])
    assert res.status_code == 201
    poll = res.json()["poll"]
    assert [o["label"] for o in poll["options"]] == ["Morning", "Evening"]
    tid = res.json()["id"]
    morning, evening = poll["options"][0]["id"], poll["options"][1]["id"]
    c = client_for(w.member2)
    assert c.post(f"{API}/threads/{tid}/vote/", {"option_id": morning}, format="json").json()["total_votes"] == 1
    out = c.post(f"{API}/threads/{tid}/vote/", {"option_id": evening}, format="json").json()
    assert out["total_votes"] == 1 and out["my_vote"] == evening
    DiscussionThread.objects.filter(pk=tid).update(poll_closes_at=timezone.now() - timedelta(minutes=1))
    assert c.post(f"{API}/threads/{tid}/vote/", {"option_id": morning}, format="json").status_code == 403
    assert new_thread(w.member, w.ca, type="poll", title="Poll with one option", poll_options=["Only"]).status_code == 400


# --- Drafts, tags, categories ------------------------------------------------------------------------------------------------


def test_drafts_are_private_and_validated(w):
    c = client_for(w.member)
    assert c.put(f"{API}/drafts/new/", {"payload": {"title": "Half written", "evil": "x"}}, format="json").status_code == 200
    assert c.get(f"{API}/drafts/new/").json()["payload"] == {"title": "Half written"}
    assert client_for(w.member2).get(f"{API}/drafts/new/").status_code == 404
    assert c.put(f"{API}/drafts/../../etc/", {"payload": {}}, format="json").status_code in (400, 404)
    assert c.put(f"{API}/drafts/bogus/", {"payload": {}}, format="json").status_code == 400
    assert c.delete(f"{API}/drafts/new/").status_code == 204


def test_tag_list_never_leaks_other_group_tags(w):
    new_thread(w.b_member, w.cb, tags=["secret-project"])
    new_thread(w.member, w.ca, title="A thread with a public tag", body="<p>tag</p>", tags=["xrd"])
    slugs = {t["slug"] for t in client_for(w.member).get(f"{API}/tags/").json()["results"]}
    assert "xrd" in slugs and "secret-project" not in slugs


def test_category_admin(w):
    admin = make_user(UserType.ADMIN)
    assert client_for(w.member).post(f"{API}/categories/", {"community": w.ca.slug, "name": "Sample prep"}, format="json").status_code == 404
    res = client_for(admin).post(f"{API}/categories/", {"community": w.ca.slug, "name": "Sample preparation"}, format="json")
    assert res.status_code == 201
    cid = res.json()["id"]
    assert client_for(admin).patch(f"{API}/categories/{cid}/", {"is_active": False}, format="json").json()["is_active"] is False
    assert ModerationAction.objects.filter(target_type="category", target_id=str(cid)).count() == 2
    assert client_for(w.member).patch(f"{API}/categories/{cid}/", {"name": "x"}, format="json").status_code == 404


# --- Attachments ----------------------------------------------------------------------------------------------------------


def upload(user, community, name="spectrum.png", data=PNG, inline=False):
    f = SimpleUploadedFile(name, data, content_type="image/png")
    return client_for(user).post(f"{API}/attachments/", {"community": community.slug, "file": f, "inline": "1" if inline else ""})


def test_attachment_upload_bind_download_and_isolation(w):
    up = upload(w.member, w.ca, inline=True)
    assert up.status_code == 201
    aid = up.json()["id"]
    assert client_for(w.member2).get(f"{API}/attachments/{aid}/").status_code == 404  # unbound: uploader only
    body = f'<p>See image</p><img data-attachment-id="{aid}" alt="trace">'
    res = new_thread(w.member, w.ca, body=body, attachments=[aid])
    assert res.status_code == 201
    opening = res.json()["opening_post"]
    assert opening["attachments"][0]["id"] == aid and f'data-attachment-id="{aid}"' in opening["body_html"]
    ok = client_for(w.member2).get(f"{API}/attachments/{aid}/")
    assert ok.status_code == 200 and ok["X-Content-Type-Options"] == "nosniff" and "no-store" in ok["Cache-Control"]
    assert "attachment" in ok["Content-Disposition"]
    assert b"".join(ok.streaming_content) == PNG
    assert client_for(w.b_member).get(f"{API}/attachments/{aid}/").status_code == 404
    assert client_for(w.external).get(f"{API}/attachments/{aid}/").status_code == 200  # external member of group A
    assert client_for().get(f"{API}/attachments/{aid}/").status_code in (401, 403)
    assert client_for(w.b_member).get(f"{API}/attachments/not-a-uuid/").status_code == 404


def test_attachment_hidden_when_post_removed(w):
    aid = upload(w.member, w.ca).json()["id"]
    tid = new_thread(w.member, w.ca, attachments=[aid]).json()["id"]
    DiscussionThread.objects.filter(pk=tid).update(visibility=Visibility.REMOVED)
    DiscussionPost.objects.filter(thread_id=tid).update(visibility=Visibility.REMOVED)
    assert client_for(w.member2).get(f"{API}/attachments/{aid}/").status_code == 404
    assert client_for(w.oic).get(f"{API}/attachments/{aid}/").status_code == 200


def test_cannot_bind_someone_elses_or_foreign_upload(w):
    theirs = upload(w.member2, w.ca, inline=True).json()["id"]
    other_comm = upload(w.member, w.general, inline=True).json()["id"]
    body = f'<p>x</p><img data-attachment-id="{theirs}"><img data-attachment-id="{other_comm}">'
    res = new_thread(w.member, w.ca, body=body, attachments=[theirs, other_comm])
    assert res.status_code == 201
    assert res.json()["opening_post"]["attachments"] == []
    assert "data-attachment-id" not in res.json()["opening_post"]["body_html"]
    assert DiscussionAttachment.objects.get(pk=theirs).post_id is None


def test_upload_type_and_size_validation(w):
    assert upload(w.member, w.ca, "tool.exe", b"MZ\x90\x00" + b"\x00" * 100).status_code == 400
    assert upload(w.member, w.ca, "fake.png", b"MZ\x90\x00" + b"\x00" * 100).status_code == 400
    assert upload(w.member, w.ca, "page.html", b"<html><script>alert(1)</script></html>").status_code == 400
    assert upload(w.member, w.ca, "logo.svg", b"<svg onload=alert(1)></svg>").status_code == 400
    assert upload(w.member, w.ca, "notes.txt", b"plain notes").status_code == 201
    assert upload(w.member, w.ca, "report.pdf", b"%PDF-1.4\n%...").status_code == 201
    assert upload(w.member, w.ca, "notes.txt", b"plain notes", inline=True).status_code == 400
    assert upload(w.member, w.cb).status_code == 404
    s = ConnectSettings.load()
    s.max_attachment_mb = 1
    s.save()
    assert upload(w.member, w.ca, "big.txt", b"a" * (1024 * 1024 + 1)).status_code == 400


def test_text_attachment_never_served_inline(w):
    aid = upload(w.member, w.ca, "notes.txt", b"<script>alert(1)</script> notes").json()["id"]
    new_thread(w.member, w.ca, attachments=[aid])
    res = client_for(w.member2).get(f"{API}/attachments/{aid}/?inline=1")
    assert res["Content-Type"] == "application/octet-stream" and "attachment" in res["Content-Disposition"]


# --- Mentions -----------------------------------------------------------------------------------------------------------


def test_mention_targets_only_readers(w):
    tid = new_thread(w.member, w.ca).json()["id"]
    thread = DiscussionThread.objects.get(pk=tid)
    html = f'<span data-mention-id="{w.member2.pk}">@a</span><span data-mention-id="{w.b_member.pk}">@b</span>'
    assert tsvc.mention_targets(html, thread, w.member) == [w.member2.pk]


def test_purge_removes_only_old_unbound_uploads(w):
    from iic_booking.connect.services.attachments import purge_unbound

    old_id = upload(w.member, w.ca, "old.txt", b"old").json()["id"]
    fresh_id = upload(w.member, w.ca, "fresh.txt", b"fresh").json()["id"]
    bound_id = upload(w.member, w.ca, "kept.txt", b"kept").json()["id"]
    new_thread(w.member, w.ca, attachments=[bound_id])
    DiscussionAttachment.objects.filter(pk__in=[old_id, bound_id]).update(created_at=timezone.now() - timedelta(days=2))
    assert purge_unbound(older_than_hours=24) == 1
    assert set(DiscussionAttachment.objects.values_list("pk", flat=True).filter(pk__in=[old_id, fresh_id, bound_id])) == {
        DiscussionAttachment.objects.get(pk=fresh_id).pk, DiscussionAttachment.objects.get(pk=bound_id).pk
    }
