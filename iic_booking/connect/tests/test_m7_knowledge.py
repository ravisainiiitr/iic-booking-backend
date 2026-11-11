"""M7: accepted solutions and the Knowledge Hub workflow (with access inheritance and leakage checks)."""

import pytest

from iic_booking.connect.models import AcceptedSolution
from iic_booking.connect.models import Community
from iic_booking.connect.models import DiscussionPost
from iic_booking.connect.models import DiscussionThread
from iic_booking.connect.models import KnowledgeArticle
from iic_booking.connect.models import KnowledgeArticleRevision
from iic_booking.connect.models import ModerationAction
from iic_booking.connect.models import Visibility
from iic_booking.connect.services import membership as msvc
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import client_for
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_external
from .conftest import make_group
from .conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def k(connect_on):
    ga, gb = make_group("Thermal Analysis"), make_group("NMR")
    ea, eb = make_equipment(ga, name="TGA"), make_equipment(gb, name="NMR 500")
    asker, helper, other_member, b_member = make_user(), make_user(), make_user(), make_user()
    op, oic = make_user(UserType.OPERATOR), make_user(UserType.MANAGER)
    ext = make_external()
    for u, e in ((asker, ea), (helper, ea), (other_member, ea), (ext, ea), (b_member, eb)):
        make_booking(u, e)
    EquipmentOperator.objects.create(equipment=ea, operator=op)
    EquipmentManager.objects.create(equipment=ea, manager=oic)
    msvc.sync_all(dry_run=False)
    ca = Community.objects.get(equipment_group_ref_id=ga.pk)
    cb = Community.objects.get(equipment_group_ref_id=gb.pk)
    res = client_for(asker).post(
        f"{API}/threads/",
        {"community": ca.slug, "type": "question", "title": "TGA mass loss below 100 C", "body": "<p>Is it moisture?</p>", "equipment_id": ea.pk},
        format="json",
    )
    tid = res.json()["id"]
    r1 = client_for(helper).post(f"{API}/threads/{tid}/posts/", {"body": "<p>Yes, dry the sample at 60 C first.</p>"}, format="json").json()["id"]
    r2 = client_for(op).post(f"{API}/threads/{tid}/posts/", {"body": "<p>Use the 10 K/min program with a nitrogen purge.</p>"}, format="json").json()["id"]
    return {"ca": ca, "cb": cb, "ea": ea, "asker": asker, "helper": helper, "other": other_member, "b": b_member, "op": op, "oic": oic,
            "ext": ext, "tid": tid, "r1": r1, "r2": r2}


def accept(user, tid, post_id):
    return client_for(user).post(f"{API}/threads/{tid}/solution/", {"post_id": post_id}, format="json")


# --- Accepted solutions -----------------------------------------------------------------------------------------------


def test_author_accepts_replaces_and_revokes_with_audit(k):
    res = accept(k["asker"], k["tid"], k["r1"])
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["is_solved"] and body["accepted_post_id"] == k["r1"]
    assert body["solution"]["solver"]["id"] == k["helper"].pk and body["solution"]["accepted_by_author"] is True
    assert body["solution"]["resolution_hours"] >= 0
    replaced = accept(k["asker"], k["tid"], k["r2"]).json()
    assert replaced["accepted_post_id"] == k["r2"] and replaced["solution"]["solver"]["badge"] == "Lab Operator (Lab In-charge)"
    assert AcceptedSolution.objects.filter(thread_id=k["tid"]).count() == 1
    revoked = client_for(k["asker"]).delete(f"{API}/threads/{k['tid']}/solution/")
    assert revoked.json()["is_solved"] is False and revoked.json()["solution"] is None
    actions = list(ModerationAction.objects.filter(target_id=str(k["tid"])).order_by("created_at").values_list("action", flat=True))
    assert actions == ["solution_accept", "solution_replace", "solution_revoke"]


def test_moderator_can_accept_but_other_member_cannot(k):
    assert accept(k["other"], k["tid"], k["r1"]).status_code == 403
    assert accept(k["helper"], k["tid"], k["r1"]).status_code == 403  # the answerer cannot accept their own answer
    assert accept(k["oic"], k["tid"], k["r1"]).status_code == 200
    assert accept(k["b"], k["tid"], k["r1"]).status_code == 404  # other group


def test_only_visible_replies_of_the_same_thread(k):
    opening = DiscussionPost.objects.get(thread_id=k["tid"], is_opening=True)
    assert accept(k["asker"], k["tid"], opening.pk).status_code == 400
    other_tid = client_for(k["asker"]).post(
        f"{API}/threads/", {"community": k["ca"].slug, "type": "question", "title": "Another TGA question here", "body": "<p>?</p>"}, format="json"
    ).json()["id"]
    other_reply = client_for(k["helper"]).post(f"{API}/threads/{other_tid}/posts/", {"body": "<p>answer</p>"}, format="json").json()["id"]
    assert accept(k["asker"], k["tid"], other_reply).status_code == 400
    DiscussionPost.objects.filter(pk=k["r1"]).update(visibility=Visibility.HIDDEN)
    assert accept(k["asker"], k["tid"], k["r1"]).status_code == 400


def test_non_solvable_types_have_no_accepted_answer(k):
    tid = client_for(k["asker"]).post(
        f"{API}/threads/", {"community": k["ca"].slug, "type": "general", "title": "General chat about TGA", "body": "<p>hi</p>"}, format="json"
    ).json()["id"]
    reply = client_for(k["helper"]).post(f"{API}/threads/{tid}/posts/", {"body": "<p>hello</p>"}, format="json").json()["id"]
    assert accept(k["asker"], tid, reply).status_code in (400, 403)
    assert client_for(k["asker"]).get(f"{API}/threads/{tid}/").json()["permissions"]["can_accept"] is False


def test_solved_filters_and_accepted_answer_cannot_be_deleted(k):
    accept(k["asker"], k["tid"], k["r1"])
    assert k["tid"] in [t["id"] for t in client_for(k["other"]).get(f"{API}/threads/?status=solved").json()["results"]]
    assert k["tid"] not in [t["id"] for t in client_for(k["other"]).get(f"{API}/threads/?status=unsolved").json()["results"]]
    assert client_for(k["helper"]).delete(f"{API}/posts/{k['r1']}/").status_code == 403


def test_accepting_never_touches_equipment(k):
    before = (k["ea"].status, k["ea"].updated_at if hasattr(k["ea"], "updated_at") else None)
    accept(k["asker"], k["tid"], k["r2"])
    k["ea"].refresh_from_db()
    assert (k["ea"].status, k["ea"].updated_at if hasattr(k["ea"], "updated_at") else None) == before


# --- Knowledge Hub -----------------------------------------------------------------------------------------------------------


def promote(user, k, **extra):
    return client_for(user).post(f"{API}/articles/", {"thread_id": k["tid"], **extra}, format="json")


def test_only_curators_promote_and_article_inherits_community(k):
    assert promote(k["other"], k).status_code == 403
    assert promote(k["b"], k).status_code == 404
    accept(k["asker"], k["tid"], k["r2"])
    res = promote(k["op"], k, summary="Dry the sample, then run 10 K/min under nitrogen.")
    assert res.status_code == 201, res.content
    art = res.json()
    assert art["status"] == "draft" and art["community"]["id"] == k["ca"].pk
    assert art["equipment"]["id"] == k["ea"].pk and art["source"]["post"]["id"] == k["r2"]
    assert "<h3>Problem</h3>" in art["body_html"] and "nitrogen purge" in art["body_html"]
    assert art["is_institutional_guidance"] is False
    # A community cannot be changed through the API (never widened).
    client_for(k["op"]).patch(f"{API}/articles/{art['id']}/", {"community": "general", "community_id": 1}, format="json")
    assert KnowledgeArticle.objects.get(pk=art["id"]).community_id == k["ca"].pk


def test_full_workflow_with_four_eyes_review(k):
    aid = promote(k["op"], k).json()["id"]
    c_op, c_oic = client_for(k["op"]), client_for(k["oic"])
    # Members cannot see a draft; the author and curators can.
    assert client_for(k["other"]).get(f"{API}/articles/{aid}/").status_code == 404
    assert c_oic.get(f"{API}/articles/{aid}/").status_code == 200
    assert c_op.post(f"{API}/articles/{aid}/publish/").status_code == 400  # not reviewed
    assert c_op.post(f"{API}/articles/{aid}/submit/").json()["status"] == "under_review"
    assert c_op.post(f"{API}/articles/{aid}/review/", {"decision": "approved"}, format="json").status_code == 403  # own article
    assert c_oic.post(f"{API}/articles/{aid}/review/", {"decision": "changes_requested"}, format="json").status_code == 400  # needs comment
    back = c_oic.post(f"{API}/articles/{aid}/review/", {"decision": "changes_requested", "comment": "Add the purge flow rate."}, format="json")
    assert back.json()["status"] == "draft"
    c_op.patch(f"{API}/articles/{aid}/", {"body": "<h3>Solution</h3><p>Nitrogen purge at 50 mL/min, 10 K/min.</p>", "note": "Flow rate"}, format="json")
    c_op.post(f"{API}/articles/{aid}/submit/")
    approved = c_oic.post(f"{API}/articles/{aid}/review/", {"decision": "approved", "institutional": True}, format="json").json()
    assert approved["status"] == "under_review" and approved["permissions"]["can_publish"] is True
    published = c_oic.post(f"{API}/articles/{aid}/publish/").json()
    assert published["status"] == "published" and published["is_institutional_guidance"] is True
    assert published["permissions"]["can_review"] is False  # the current version is already approved
    assert published["reviewer"]["id"] == k["oic"].pk and published["last_reviewed_at"]
    # Members now read it; Group B never does.
    assert client_for(k["other"]).get(f"{API}/articles/{aid}/").status_code == 200
    assert client_for(k["ext"]).get(f"{API}/articles/{aid}/").status_code == 200
    assert client_for(k["b"]).get(f"{API}/articles/{aid}/").status_code == 404
    assert aid not in [a["id"] for a in client_for(k["b"]).get(f"{API}/articles/").json()["results"]]
    # Editing a published article keeps it published but drops the guidance label until re-reviewed.
    edited = c_op.patch(f"{API}/articles/{aid}/", {"summary": "Updated after the new SOP."}, format="json").json()
    assert edited["status"] == "published" and edited["is_institutional_guidance"] is False and edited["needs_review"] is True
    assert c_oic.get(f"{API}/articles/{aid}/").json()["permissions"]["can_review"] is True
    re_reviewed = c_oic.post(f"{API}/articles/{aid}/review/", {"decision": "approved", "institutional": True}, format="json").json()
    assert re_reviewed["is_institutional_guidance"] is True
    assert KnowledgeArticleRevision.objects.filter(article_id=aid).count() >= 3
    revs = c_op.get(f"{API}/articles/{aid}/revisions/").json()["results"]
    assert revs[0]["version"] > revs[-1]["version"]
    assert client_for(k["other"]).get(f"{API}/articles/{aid}/revisions/").status_code == 404


def test_archive_hides_from_members_and_restore(k):
    aid = promote(k["op"], k).json()["id"]
    client_for(k["op"]).post(f"{API}/articles/{aid}/submit/")
    client_for(k["oic"]).post(f"{API}/articles/{aid}/review/", {"decision": "approved"}, format="json")
    client_for(k["oic"]).post(f"{API}/articles/{aid}/publish/")
    assert client_for(k["oic"]).post(f"{API}/articles/{aid}/archive/", {"reason": "Superseded"}, format="json").json()["status"] == "archived"
    assert client_for(k["other"]).get(f"{API}/articles/{aid}/").status_code == 404
    assert client_for(k["oic"]).post(f"{API}/articles/{aid}/restore/").json()["status"] == "draft"


def test_community_reply_is_never_presented_as_guidance(k):
    aid = promote(k["op"], k, post_id=k["r1"]).json()["id"]  # community member's reply
    client_for(k["op"]).post(f"{API}/articles/{aid}/submit/")
    client_for(k["oic"]).post(f"{API}/articles/{aid}/review/", {"decision": "approved", "institutional": False}, format="json")
    published = client_for(k["oic"]).post(f"{API}/articles/{aid}/publish/").json()
    assert published["is_institutional_guidance"] is False
    assert published["source"]["post"]["is_verified_response"] is False


def test_article_search_and_home_section_respect_access(k):
    aid = promote(k["op"], k, title="Removing moisture before TGA runs").json()["id"]
    client_for(k["op"]).post(f"{API}/articles/{aid}/submit/")
    client_for(k["oic"]).post(f"{API}/articles/{aid}/review/", {"decision": "approved"}, format="json")
    client_for(k["oic"]).post(f"{API}/articles/{aid}/publish/")
    assert [a["id"] for a in client_for(k["other"]).get(f"{API}/search/", {"q": "moisture"}).json()["articles"]] == [aid]
    assert client_for(k["b"]).get(f"{API}/search/", {"q": "moisture"}).json()["articles"] == []
    assert [a["id"] for a in client_for(k["other"]).get(f"{API}/home/").json()["articles"]] == [aid]
    assert client_for(k["b"]).get(f"{API}/home/").json()["articles"] == []
    assert [a["id"] for a in client_for(k["other"]).get(f"{API}/articles/", {"q": "moisture"}).json()["results"]] == [aid]


def test_thread_shows_promoted_article_link(k):
    aid = promote(k["op"], k).json()["id"]
    detail = client_for(k["op"]).get(f"{API}/threads/{k['tid']}/").json()
    assert detail["articles"][0]["id"] == aid and detail["permissions"]["can_curate"] is True
    assert client_for(k["other"]).get(f"{API}/threads/{k['tid']}/").json()["articles"] == []  # drafts are not shown to members
    assert DiscussionThread.objects.filter(pk=k["tid"]).exists()


# --- Resolved by the person who asked ---------------------------------------------------------------------------


def test_originator_accepting_marks_resolved(k):
    body = accept(k["asker"], k["tid"], k["r2"]).json()
    assert body["is_solved"] and body["is_resolved"] is True
    assert body["solution"]["resolved_by_author"] is True and body["solution"]["can_confirm_resolved"] is False
    ids = [t["id"] for t in client_for(k["other"]).get(f"{API}/threads/?status=resolved").json()["results"]]
    assert k["tid"] in ids


def test_moderator_accepted_answer_needs_originator_confirmation(k):
    body = accept(k["oic"], k["tid"], k["r2"]).json()
    assert body["is_solved"] and body["is_resolved"] is False
    assert k["tid"] not in [t["id"] for t in client_for(k["other"]).get(f"{API}/threads/?status=resolved").json()["results"]]
    asker_view = client_for(k["asker"]).get(f"{API}/threads/{k['tid']}/").json()
    assert asker_view["solution"]["can_confirm_resolved"] is True
    assert client_for(k["other"]).put(f"{API}/threads/{k['tid']}/solution/").status_code == 403
    assert client_for(k["oic"]).put(f"{API}/threads/{k['tid']}/solution/").status_code == 403  # only the asker confirms
    confirmed = client_for(k["asker"]).put(f"{API}/threads/{k['tid']}/solution/").json()
    assert confirmed["is_resolved"] is True and confirmed["solution"]["resolved_at"]
    assert ModerationAction.objects.filter(action="solution_confirm_resolved", target_id=str(k["tid"])).exists()


def test_replacing_or_revoking_clears_resolved(k):
    accept(k["asker"], k["tid"], k["r1"])
    replaced = accept(k["oic"], k["tid"], k["r2"]).json()
    assert replaced["is_solved"] and replaced["is_resolved"] is False
    accept(k["asker"], k["tid"], k["r2"])  # re-accepting the same answer as the asker confirms it
    assert client_for(k["asker"]).get(f"{API}/threads/{k['tid']}/").json()["is_resolved"] is True
    revoked = client_for(k["asker"]).delete(f"{API}/threads/{k['tid']}/solution/").json()
    assert revoked["is_resolved"] is False and revoked["is_solved"] is False


def test_confirm_without_accepted_answer_is_refused(k):
    assert client_for(k["asker"]).put(f"{API}/threads/{k['tid']}/solution/").status_code == 400
