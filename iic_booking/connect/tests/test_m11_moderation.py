"""M11: reports, moderator actions, lock, move, queue, dashboard, audit log, Community Admins."""

import pytest

from iic_booking.connect.models import Community
from iic_booking.connect.models import ConnectRoleAssignment
from iic_booking.connect.models import DiscussionPost
from iic_booking.connect.models import DiscussionReport
from iic_booking.connect.models import DiscussionThread
from iic_booking.connect.models import ModerationAction
from iic_booking.connect.models import ReportStatus
from iic_booking.connect.models import Visibility
from iic_booking.connect.services import membership as msvc
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


@pytest.fixture
def m(connect_on):
    g, gb = make_group("Thin films"), make_group("Bio")
    eq, eqb = make_equipment(g, name="Sputter"), make_equipment(gb, name="FACS")
    users = [make_user(name=f"Member {i}") for i in range(4)]
    for u in users:
        make_booking(u, eq)
    b_user = make_user(name="Bio user")
    make_booking(b_user, eqb)
    op, oic, b_oic = make_user(UserType.OPERATOR), make_user(UserType.MANAGER), make_user(UserType.MANAGER)
    EquipmentOperator.objects.create(equipment=eq, operator=op)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentManager.objects.create(equipment=eqb, manager=b_oic)
    msvc.sync_all(dry_run=False)
    c = Community.objects.get(equipment_group_ref_id=g.pk)
    cb = Community.objects.get(equipment_group_ref_id=gb.pk)
    tid = client_for(users[0]).post(f"{API}/threads/", {"community": c.slug, "title": "Sputter target cracking", "body": "<p>Why?</p>", "type": "question"}, format="json").json()["id"]
    rid = client_for(users[1]).post(f"{API}/threads/{tid}/posts/", {"body": "<p>Buy cheap targets from my shop!</p>"}, format="json").json()["id"]
    return {"c": c, "cb": cb, "users": users, "b_user": b_user, "op": op, "oic": oic, "b_oic": b_oic, "tid": tid, "rid": rid}


def report(user, pid, reason="spam"):
    return client_for(user).post(f"{API}/posts/{pid}/report/", {"reason": reason, "details": "ad"}, format="json")


def moderate(user, pid, action, reason="Spam"):
    return client_for(user).post(f"{API}/posts/{pid}/moderate/", {"action": action, "reason": reason}, format="json")


def test_report_rules_and_auto_hide(m):
    u = m["users"]
    assert report(u[1], m["rid"]).status_code == 400  # own post
    assert report(u[0], m["rid"], reason="bogus").status_code == 400
    assert report(u[0], m["rid"]).status_code == 201
    assert report(u[0], m["rid"]).status_code == 400  # once per post
    assert report(m["b_user"], m["rid"]).status_code == 404  # other group
    report(u[2], m["rid"])
    assert DiscussionPost.objects.get(pk=m["rid"]).visibility == Visibility.VISIBLE
    report(u[3], m["rid"])  # third distinct reporter
    assert DiscussionPost.objects.get(pk=m["rid"]).visibility == Visibility.HIDDEN
    assert ModerationAction.objects.filter(action="auto_hide", target_id=str(m["rid"])).exists()
    assert m["rid"] not in [p["id"] for p in client_for(u[2]).get(f"{API}/threads/{m['tid']}/posts/").json()["results"]]


def test_moderator_hide_remove_restore_with_audit_and_notice(m, django_capture_on_commit_callbacks):
    assert moderate(m["users"][0], m["rid"], "hide").status_code == 403
    assert moderate(m["b_oic"], m["rid"], "hide").status_code == 404  # moderator of another group
    assert moderate(m["op"], m["rid"], "hide", reason="").status_code == 400  # reason required
    report(m["users"][0], m["rid"])
    with django_capture_on_commit_callbacks(execute=True):
        assert moderate(m["op"], m["rid"], "remove").json()["visibility"] == "removed"
    assert DiscussionReport.objects.get(post_id=m["rid"]).status == ReportStatus.ACTIONED
    from iic_booking.connect.models import DiscussionNotification
    from iic_booking.connect.models import NotificationEvent

    assert DiscussionNotification.objects.filter(recipient=m["users"][1], event=NotificationEvent.MODERATION).exists()
    assert moderate(m["oic"], m["rid"], "restore", reason="").json()["visibility"] == "visible"
    actions = list(ModerationAction.objects.filter(target_id=str(m["rid"])).order_by("created_at").values_list("action", flat=True))
    assert actions == ["post_remove", "post_restore"]


def test_hiding_accepted_answer_unsolves_thread(m):
    client_for(m["users"][0]).post(f"{API}/threads/{m['tid']}/solution/", {"post_id": m["rid"]}, format="json")
    assert DiscussionThread.objects.get(pk=m["tid"]).solved_at is not None
    moderate(m["op"], m["rid"], "hide")
    t = DiscussionThread.objects.get(pk=m["tid"])
    assert t.solved_at is None and t.accepted_post_id is None


def test_hiding_opening_post_hides_thread(m):
    opening = DiscussionPost.objects.get(thread_id=m["tid"], is_opening=True)
    moderate(m["oic"], opening.pk, "hide")
    assert DiscussionThread.objects.get(pk=m["tid"]).visibility == Visibility.HIDDEN
    assert client_for(m["users"][2]).get(f"{API}/threads/{m['tid']}/").status_code == 404
    assert client_for(m["users"][0]).get(f"{API}/threads/{m['tid']}/").status_code == 200  # author still sees it (hidden)


def test_lock_and_move_category(m):
    assert client_for(m["users"][0]).post(f"{API}/threads/{m['tid']}/lock/", {"locked": True}, format="json").status_code == 403
    assert client_for(m["op"]).post(f"{API}/threads/{m['tid']}/lock/", {"locked": True}, format="json").json()["status"] == "locked"
    from iic_booking.connect.models import DiscussionCategory

    cat = DiscussionCategory.objects.create(community=m["c"], name="Targets", slug="targets")
    moved = client_for(m["op"]).post(f"{API}/threads/{m['tid']}/move/", {"category_id": cat.pk}, format="json").json()
    assert moved["category"]["slug"] == "targets"
    assert client_for(m["op"]).post(f"{API}/threads/{m['tid']}/move/", {"community_id": general_community().pk}, format="json").status_code == 403


def test_admin_move_across_communities_needs_confirmation(m):
    admin = make_user(UserType.ADMIN)
    general = general_community()
    url = f"{API}/threads/{m['tid']}/move/"
    res = client_for(admin).post(url, {"community_id": general.pk}, format="json")
    assert res.status_code == 400 and res.json()["field"] == "confirm_widen"
    assert DiscussionThread.objects.get(pk=m["tid"]).community_id == m["c"].pk
    res = client_for(admin).post(url, {"community_id": general.pk, "confirm_widen": True, "reason": "Of general interest"}, format="json")
    assert res.status_code == 200 and res.json()["community"]["kind"] == "general"
    assert ModerationAction.objects.filter(action="thread_move", target_id=str(m["tid"])).exists()


def test_queue_dashboard_and_log_are_scoped(m):
    report(m["users"][0], m["rid"])
    q = client_for(m["op"]).get(f"{API}/moderation/queue/").json()
    assert [r["post_id"] for r in q["reported"]] == [m["rid"]] and q["reported"][0]["open_reports"] == 1
    assert client_for(m["b_oic"]).get(f"{API}/moderation/queue/").json()["reported"] == []
    assert client_for(m["users"][0]).get(f"{API}/moderation/queue/").status_code == 404
    dash = client_for(m["op"]).get(f"{API}/moderation/dashboard/").json()
    assert dash["totals"]["open_reports"] == 1
    moderate(m["op"], m["rid"], "hide")
    assert client_for(m["b_oic"]).get(f"{API}/moderation/log/").json()["results"] == []
    assert any(r["action"] == "post_hide" for r in client_for(m["oic"]).get(f"{API}/moderation/log/").json()["results"])
    assert client_for(m["users"][0]).get(f"{API}/moderation/log/").status_code == 404


def test_dismiss_reports(m):
    report(m["users"][0], m["rid"])
    res = client_for(m["op"]).post(f"{API}/posts/{m['rid']}/moderate/", {"action": "dismiss_reports", "reason": "Not spam"}, format="json")
    assert res.status_code == 200
    assert DiscussionReport.objects.get(post_id=m["rid"]).status == ReportStatus.DISMISSED


def test_community_admins_by_portal_admin_only(m):
    admin = make_user(UserType.ADMIN)
    target = make_user(email="moderator.one@iitr.ac.in")
    assert client_for(m["oic"]).post(f"{API}/moderation/community-admins/", {"email": target.email}, format="json").status_code == 404
    res = client_for(admin).post(f"{API}/moderation/community-admins/", {"email": target.email}, format="json")
    assert [r["user_id"] for r in res.json()["results"]] == [target.pk]
    ext = make_external(email="partner@corp.example")
    assert client_for(admin).post(f"{API}/moderation/community-admins/", {"email": ext.email}, format="json").status_code == 400
    assert client_for(target).get(f"{API}/moderation/queue/").status_code == 200  # can now moderate everywhere
    client_for(admin).delete(f"{API}/moderation/community-admins/", {"user_id": target.pk}, format="json")
    assert not ConnectRoleAssignment.objects.filter(user=target, revoked_at__isnull=True).exists()
