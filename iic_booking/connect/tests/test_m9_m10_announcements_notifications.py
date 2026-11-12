"""M9 announcements and M10 notifications (targeting, acknowledgement, access re-checks, digests, preferences)."""

from datetime import timedelta

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.communication.models import CommunicationLog
from iic_booking.connect.models import Community
from iic_booking.connect.models import CommunityMembership
from iic_booking.connect.models import ConnectNotificationPreference
from iic_booking.connect.models import DiscussionNotification
from iic_booking.connect.models import DiscussionSubscription
from iic_booking.connect.models import EmailMode
from iic_booking.connect.models import MembershipStatus
from iic_booking.connect.models import NotificationEvent
from iic_booking.connect.services import membership as msvc
from iic_booking.connect.services import notifications as nsvc
from iic_booking.equipment.models import Booking
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import client_for
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_group
from .conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def n(connect_on):
    g, gb = make_group("Mass Spec"), make_group("Other group")
    eq1, eq2, eqb = make_equipment(g, name="LC-MS"), make_equipment(g, name="GC-MS"), make_equipment(gb, name="XPS")
    asker, helper, gc_user, other_group = make_user(name="Asha"), make_user(name="Hari"), make_user(name="Gita"), make_user(name="Omar")
    op = make_user(UserType.OPERATOR, name="Op Ravi")
    make_booking(asker, eq1)
    make_booking(helper, eq1)
    make_booking(gc_user, eq2)
    make_booking(other_group, eqb)
    EquipmentOperator.objects.create(equipment=eq1, operator=op)
    msvc.sync_all(dry_run=False)
    return {"c": Community.objects.get(equipment_group_ref_id=g.pk), "cb": Community.objects.get(equipment_group_ref_id=gb.pk),
            "eq1": eq1, "eq2": eq2, "eqb": eqb, "asker": asker, "helper": helper, "gc": gc_user, "other": other_group, "op": op}


def create(user, community, title, cb, **extra):
    with cb(execute=True):
        res = client_for(user).post(f"{API}/threads/", {"community": community.slug, "title": title, "body": f"<p>{title} secret body text</p>", **extra}, format="json")
    assert res.status_code == 201, res.content
    return res.json()


def reply(user, tid, body, cb, **extra):
    with cb(execute=True):
        res = client_for(user).post(f"{API}/threads/{tid}/posts/", {"body": body, **extra}, format="json")
    assert res.status_code == 201, res.content
    return res.json()["id"]


def events_for(user):
    return list(DiscussionNotification.objects.filter(recipient=user).values_list("event", flat=True))


# --- M9 announcements ---------------------------------------------------------------------------------------------


def test_only_staff_post_announcements_with_settings(n, django_capture_on_commit_callbacks):
    res = client_for(n["asker"]).post(f"{API}/threads/", {"community": n["c"].slug, "type": "announcement", "title": "Not allowed here", "body": "<p>x</p>"}, format="json")
    assert res.status_code == 403
    expires = (timezone.now() + timedelta(days=3)).isoformat()
    body = create(n["op"], n["c"], "LC-MS column change on Friday", django_capture_on_commit_callbacks, type="announcement",
                  priority="important", expires_at=expires, requires_acknowledgement=True, target_equipment_id=n["eq1"].pk, pin=True)
    ann = body["announcement"]
    assert body["is_pinned"] and ann["priority"] == "important" and ann["requires_acknowledgement"]
    assert ann["target_equipment"]["id"] == n["eq1"].pk and ann["is_expired"] is False
    assert ann["audience_count"] == 2  # bookers of LC-MS (asker, helper); the author is excluded


def test_announcement_validation(n):
    past = (timezone.now() - timedelta(hours=1)).isoformat()
    c = client_for(n["op"])
    base = {"community": n["c"].slug, "type": "announcement", "title": "Validation announcement", "body": "<p>x</p>"}
    assert c.post(f"{API}/threads/", {**base, "expires_at": past}, format="json").status_code == 400
    assert c.post(f"{API}/threads/", {**base, "target_equipment_id": n["eqb"].pk}, format="json").status_code == 400
    assert c.post(f"{API}/threads/", {**base, "priority": "urgentest"}, format="json").status_code == 400
    # Nothing half-created by the failed attempts.
    assert not n["c"].threads.filter(title="Validation announcement").exists()


def test_targeted_announcement_notifies_only_that_equipments_users(n, django_capture_on_commit_callbacks):
    create(n["op"], n["c"], "LC-MS maintenance", django_capture_on_commit_callbacks, type="announcement", target_equipment_id=n["eq1"].pk)
    assert events_for(n["asker"]) == [NotificationEvent.ANNOUNCEMENT]
    assert events_for(n["helper"]) == [NotificationEvent.ANNOUNCEMENT]
    assert events_for(n["gc"]) == []  # GC-MS user: same community, other instrument
    assert events_for(n["other"]) == []


def test_acknowledgement(n, django_capture_on_commit_callbacks):
    tid = create(n["op"], n["c"], "Read the new SOP please", django_capture_on_commit_callbacks, type="announcement", requires_acknowledgement=True)["id"]
    res = client_for(n["asker"]).post(f"{API}/threads/{tid}/acknowledge/")
    assert res.status_code == 200 and res.json()["announcement"]["acknowledged"] is True
    client_for(n["asker"]).post(f"{API}/threads/{tid}/acknowledge/")  # idempotent
    staff_view = client_for(n["op"]).get(f"{API}/threads/{tid}/").json()["announcement"]
    assert staff_view["acknowledged_count"] == 1
    assert [r["user_id"] for r in client_for(n["op"]).get(f"{API}/threads/{tid}/acknowledge/").json()["results"]] == [n["asker"].pk]
    assert client_for(n["asker"]).get(f"{API}/threads/{tid}/acknowledge/").status_code == 403
    assert "acknowledged_count" not in client_for(n["asker"]).get(f"{API}/threads/{tid}/").json()["announcement"]
    assert client_for(n["other"]).post(f"{API}/threads/{tid}/acknowledge/").status_code == 404


def test_expired_announcements_leave_home_and_panel(n, django_capture_on_commit_callbacks):
    tid = create(n["op"], n["c"], "Short notice announcement", django_capture_on_commit_callbacks, type="announcement",
                 expires_at=(timezone.now() + timedelta(minutes=5)).isoformat(), target_equipment_id=n["eq1"].pk)["id"]
    from iic_booking.connect.models import CommunityAnnouncement

    assert tid in [t["id"] for t in client_for(n["asker"]).get(f"{API}/home/").json()["announcements"]]
    CommunityAnnouncement.objects.filter(thread_id=tid).update(expires_at=timezone.now() - timedelta(minutes=1))
    assert tid not in [t["id"] for t in client_for(n["asker"]).get(f"{API}/home/").json()["announcements"]]
    assert client_for(n["asker"]).get(f"{API}/threads/{tid}/").json()["announcement"]["is_expired"] is True


def test_announcements_never_touch_bookings(n, django_capture_on_commit_callbacks):
    before = list(Booking.objects.order_by("pk").values_list("pk", "status"))
    create(n["op"], n["c"], "Instrument down notice", django_capture_on_commit_callbacks, type="announcement", priority="critical", pin=True)
    assert list(Booking.objects.order_by("pk").values_list("pk", "status")) == before


# --- M10 notifications --------------------------------------------------------------------------------------------------


def test_reply_reply_to_me_and_mention_with_dedupe(n, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "LC-MS sensitivity drop", django_capture_on_commit_callbacks)["id"]
    first = reply(n["helper"], tid, "<p>Clean the source.</p>", django_capture_on_commit_callbacks)
    assert events_for(n["asker"]) == [NotificationEvent.REPLY]
    assert events_for(n["helper"]) == []  # never notified about your own post
    mention = f'<p>Agree, see also <span data-mention-id="{n["op"].pk}">@Op</span> and <span data-mention-id="{n["other"].pk}">@Omar</span></p>'
    reply(n["gc"], tid, mention, django_capture_on_commit_callbacks, parent_id=first)
    assert NotificationEvent.REPLY_TO_ME in events_for(n["helper"])
    assert NotificationEvent.MENTION in events_for(n["op"])
    assert events_for(n["other"]) == []  # mentioned but cannot read the thread
    # The asker (thread author, auto-following) gets one notification per post, not two.
    assert DiscussionNotification.objects.filter(recipient=n["asker"]).count() == 2


def test_followers_and_muted(n, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "Thread to follow", django_capture_on_commit_callbacks)["id"]
    DiscussionSubscription.objects.create(user=n["gc"], thread_id=tid, level="watching")
    DiscussionSubscription.objects.update_or_create(user=n["asker"], thread_id=tid, defaults={"level": "muted"})
    reply(n["helper"], tid, "<p>reply</p>", django_capture_on_commit_callbacks)
    assert events_for(n["gc"]) == [NotificationEvent.FOLLOWED]
    assert events_for(n["asker"]) == []  # muted
    DiscussionSubscription.objects.create(user=n["op"], community=n["c"], level="watching")
    create(n["helper"], n["c"], "New thread in followed community", django_capture_on_commit_callbacks)
    assert NotificationEvent.FOLLOWED in events_for(n["op"])


def test_accepted_answer_notifies_solver(n, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "Question to be solved", django_capture_on_commit_callbacks, type="question")["id"]
    pid = reply(n["helper"], tid, "<p>answer</p>", django_capture_on_commit_callbacks)
    with django_capture_on_commit_callbacks(execute=True):
        client_for(n["asker"]).post(f"{API}/threads/{tid}/solution/", {"post_id": pid}, format="json")
    assert NotificationEvent.ACCEPTED in events_for(n["helper"])


def test_no_notification_after_losing_access(n, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "Revoked user thread", django_capture_on_commit_callbacks)["id"]
    CommunityMembership.objects.filter(community=n["c"], user=n["asker"]).update(status=MembershipStatus.REVOKED)
    reply(n["helper"], tid, "<p>reply after revoke</p>", django_capture_on_commit_callbacks)
    assert events_for(n["asker"]) == []


def test_in_app_uses_the_portal_bell_without_post_bodies(n, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "Bell delivery thread", django_capture_on_commit_callbacks)["id"]
    before = CommunicationLog.objects.filter(recipient=n["asker"]).count()
    reply(n["helper"], tid, "<p>Confidential reply text</p>", django_capture_on_commit_callbacks)
    note = DiscussionNotification.objects.get(recipient=n["asker"])
    assert note.in_app_sent_at is not None
    logs = CommunicationLog.objects.filter(recipient=n["asker"]).order_by("-pk")
    assert logs.count() == before + 1
    log = logs.first()
    assert "Confidential reply text" not in (log.message or "") and (log.metadata or {}).get("notification_category") == "discussions"


def test_immediate_email_has_title_and_link_only(n, django_capture_on_commit_callbacks):
    ConnectNotificationPreference.objects.create(user=n["asker"], email_mode=EmailMode.IMMEDIATE)
    tid = create(n["asker"], n["c"], "Email delivery thread", django_capture_on_commit_callbacks)["id"]
    mail.outbox.clear()
    reply(n["helper"], tid, "<p>Do not email this body</p>", django_capture_on_commit_callbacks)
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    full = msg.body + "".join(str(a[0]) for a in getattr(msg, "alternatives", []))
    assert "Email delivery thread" in full and f"/connect/t/{tid}" in full
    assert "Do not email this body" not in full


def test_daily_digest_rechecks_access(n, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "Digest thread one", django_capture_on_commit_callbacks)["id"]
    reply(n["helper"], tid, "<p>r1</p>", django_capture_on_commit_callbacks)
    reply(n["gc"], tid, "<p>r2</p>", django_capture_on_commit_callbacks)
    mail.outbox.clear()
    # The asker (two replies) and the helper (auto-followed by replying, then notified of r2) each get one digest.
    assert nsvc.send_digests(EmailMode.DAILY) == 2
    to_asker = [m for m in mail.outbox if n["asker"].email in m.to]
    assert len(to_asker) == 1 and "Digest thread one" in to_asker[0].body
    assert not DiscussionNotification.objects.filter(email_mode=EmailMode.DAILY, email_sent_at__isnull=True).exists()
    # A user who lost access before the digest gets nothing about it.
    tid2 = create(n["helper"], n["c"], "Digest thread two", django_capture_on_commit_callbacks)["id"]
    reply(n["gc"], tid2, "<p>r3</p>", django_capture_on_commit_callbacks)
    CommunityMembership.objects.filter(community=n["c"], user=n["helper"]).update(status=MembershipStatus.REVOKED)
    mail.outbox.clear()
    nsvc.send_digests(EmailMode.DAILY)
    assert all("Digest thread two" not in m.body for m in mail.outbox)


def test_preferences_api_and_muted_events(n, django_capture_on_commit_callbacks):
    c = client_for(n["asker"])
    assert c.get(f"{API}/notifications/preferences/").json()["email_mode"] == "daily"
    assert c.put(f"{API}/notifications/preferences/", {"email_mode": "hourly"}, format="json").status_code == 400
    res = c.put(f"{API}/notifications/preferences/", {"email_mode": "weekly", "muted_events": ["reply"], "in_app_enabled": False}, format="json")
    assert res.json()["email_mode"] == "weekly" and res.json()["muted_events"] == ["reply"]
    tid = create(n["asker"], n["c"], "Muted reply events thread", django_capture_on_commit_callbacks)["id"]
    reply(n["helper"], tid, "<p>muted</p>", django_capture_on_commit_callbacks)
    assert events_for(n["asker"]) == []


def test_inbox_lists_only_readable_and_marks_read(n, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "Inbox thread", django_capture_on_commit_callbacks)["id"]
    reply(n["helper"], tid, "<p>r</p>", django_capture_on_commit_callbacks)
    c = client_for(n["asker"])
    data = c.get(f"{API}/notifications/").json()
    assert data["unread_count"] == 1 and data["results"][0]["link"].startswith(f"/connect/t/{tid}")
    assert c.post(f"{API}/notifications/read/", {"all": True}, format="json").json()["marked"] == 1
    CommunityMembership.objects.filter(community=n["c"], user=n["asker"]).update(status=MembershipStatus.REVOKED)
    assert client_for(n["asker"]).get(f"{API}/notifications/").json()["results"] == []


def test_flag_off_sends_nothing(n, settings, django_capture_on_commit_callbacks):
    tid = create(n["asker"], n["c"], "Flag off thread", django_capture_on_commit_callbacks)["id"]
    settings.IITR_CONNECT_ENABLED = False
    from iic_booking.connect.models import DiscussionPost
    from iic_booking.connect.models import DiscussionThread
    from iic_booking.connect.services.notifications import post_created

    post = DiscussionPost.objects.filter(thread_id=tid).first()
    with django_capture_on_commit_callbacks(execute=True):
        post_created(DiscussionThread.objects.get(pk=tid), post, "")
    assert DiscussionNotification.objects.count() == 0
