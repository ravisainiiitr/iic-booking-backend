"""M12: permission-scoped analytics and manipulation-resistant recognition."""

import pytest

from iic_booking.connect.models import Community
from iic_booking.connect.models import ConnectSearchLog
from iic_booking.connect.models import ConnectSettings
from iic_booking.connect.models import DiscussionReaction
from iic_booking.connect.models import RecognitionBadge
from iic_booking.connect.models import UserBadgeAward
from iic_booking.connect.services import membership as msvc
from iic_booking.connect.services import recognition
from iic_booking.equipment.models import EquipmentManager
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import client_for
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_group
from .conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def a(connect_on):
    g, gb = make_group("Calorimetry"), make_group("Optics")
    eq, eqb = make_equipment(g, name="DSC"), make_equipment(gb, name="Ellipsometer")
    asker, helper, fan = make_user(name="Asker"), make_user(name="Helper"), make_user(name="Fan")
    b_user = make_user(name="Optics user")
    for u in (asker, helper, fan):
        make_booking(u, eq)
    make_booking(b_user, eqb)
    oic, b_oic = make_user(UserType.MANAGER), make_user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentManager.objects.create(equipment=eqb, manager=b_oic)
    msvc.sync_all(dry_run=False)
    return {"c": Community.objects.get(equipment_group_ref_id=g.pk), "cb": Community.objects.get(equipment_group_ref_id=gb.pk),
            "eq": eq, "eqb": eqb, "asker": asker, "helper": helper, "fan": fan, "b": b_user, "oic": oic, "b_oic": b_oic}


def thread(user, community, title, **extra):
    return client_for(user).post(f"{API}/threads/", {"community": community.slug, "title": title, "body": f"<p>{title}</p>", **extra}, format="json").json()["id"]


def reply(user, tid, body="<p>answer</p>"):
    return client_for(user).post(f"{API}/threads/{tid}/posts/", {"body": body}, format="json").json()["id"]


# --- Analytics ---------------------------------------------------------------------------------------------------------


def test_analytics_only_for_moderators_and_scoped(a):
    thread(a["asker"], a["c"], "DSC baseline question one", type="question", equipment_id=a["eq"].pk)
    thread(a["b"], a["cb"], "Optics secret question", type="technical_issue", equipment_id=a["eqb"].pk)
    assert client_for(a["asker"]).get(f"{API}/analytics/").status_code == 404
    data = client_for(a["oic"]).get(f"{API}/analytics/").json()
    assert data["scope"]["community_ids"] == [a["c"].pk]
    assert data["totals"]["threads"] == 1 and data["totals"]["open_questions"] == 1
    assert [r["name"] for r in data["most_discussed_equipment"]] == ["DSC"]
    assert "Optics secret question" not in str(data)
    assert "downtime" in data["note"]
    # Asking for another community narrows to nothing rather than leaking it.
    other = client_for(a["oic"]).get(f"{API}/analytics/", {"community": a["cb"].pk}).json()
    assert other["scope"]["community_ids"] == [] and other["totals"]["threads"] == 0


def test_search_insight_is_admin_only_with_threshold(a):
    for _ in range(3):
        ConnectSearchLog.objects.create(user=a["asker"], query="dsc calibration", kind="search")
    ConnectSearchLog.objects.create(user=a["asker"], query="rare one-off query", kind="search")
    assert client_for(a["oic"]).get(f"{API}/analytics/").json()["top_searches"] == []
    admin = make_user(UserType.ADMIN)
    rows = client_for(admin).get(f"{API}/analytics/").json()["top_searches"]
    assert rows == [{"query": "dsc calibration", "count": 3}]


def test_recurring_problems_and_unanswered(a):
    for i in range(2):
        thread(a["asker"], a["c"], f"DSC sensor fault number {i}", type="technical_issue", equipment_id=a["eq"].pk)
    data = client_for(a["oic"]).get(f"{API}/analytics/").json()
    assert data["recurring_problems"][0]["technical_issues"] == 2
    assert len(data["frequently_unanswered"]) == 2


# --- Recognition -------------------------------------------------------------------------------------------------------


def enable_recognition():
    s = ConnectSettings.load()
    s.recognition_enabled = True
    s.save()


def test_recognition_off_by_default_awards_nothing(a):
    assert recognition.recompute()["enabled"] is False
    assert not UserBadgeAward.objects.exists()


def test_badges_come_from_accepted_answers_not_volume(a):
    enable_recognition()
    RecognitionBadge.objects.filter(code="technical-expert").update(criteria={"accepted_answers": 2})
    for i in range(2):
        tid = thread(a["asker"], a["c"], f"Accepted question number {i}", type="question")
        pid = reply(a["helper"], tid, f"<p>Distinct answer {i}</p>")
        client_for(a["asker"]).post(f"{API}/threads/{tid}/solution/", {"post_id": pid}, format="json")
    for i in range(10):  # a lot of posting, no accepted answers
        reply(a["fan"], tid, f"<p>chatter {i}</p>")
    recognition.recompute()
    codes = lambda u: set(UserBadgeAward.objects.filter(user=u, revoked_at__isnull=True).values_list("badge__code", flat=True))
    assert "technical-expert" in codes(a["helper"])
    assert codes(a["fan"]) == set()


def test_self_accepted_answers_do_not_count(a):
    enable_recognition()
    RecognitionBadge.objects.filter(code="technical-expert").update(criteria={"accepted_answers": 1})
    tid = thread(a["asker"], a["c"], "I will answer myself", type="question")
    pid = reply(a["asker"], tid)
    client_for(a["asker"]).post(f"{API}/threads/{tid}/solution/", {"post_id": pid}, format="json")
    recognition.recompute()
    assert not UserBadgeAward.objects.filter(user=a["asker"]).exists()


def test_helpful_counts_distinct_people_once(a):
    enable_recognition()
    RecognitionBadge.objects.filter(code="helpful-contributor").update(criteria={"distinct_helpful_users": 2})
    tid = thread(a["asker"], a["c"], "Helpful counting thread")
    posts = [reply(a["helper"], tid, f"<p>tip {i}</p>") for i in range(5)]
    for pid in posts:  # one fan marking every post counts once
        client_for(a["fan"]).post(f"{API}/posts/{pid}/react/", {"kind": "helpful"}, format="json")
    recognition.recompute()
    assert not UserBadgeAward.objects.filter(user=a["helper"], badge__code="helpful-contributor").exists()
    client_for(a["asker"]).post(f"{API}/posts/{posts[0]}/react/", {"kind": "helpful"}, format="json")
    recognition.recompute()
    assert UserBadgeAward.objects.filter(user=a["helper"], badge__code="helpful-contributor", revoked_at__isnull=True).exists()
    # Withdrawn when the evidence disappears.
    DiscussionReaction.objects.filter(user=a["asker"]).delete()
    recognition.recompute()
    assert UserBadgeAward.objects.get(user=a["helper"], badge__code="helpful-contributor").revoked_at is not None


def test_settings_portal_admin_only_and_validated(a):
    admin = make_user(UserType.ADMIN)
    assert client_for(a["oic"]).get(f"{API}/admin/settings/").status_code == 404
    assert client_for(admin).patch(f"{API}/admin/settings/", {"max_attachment_mb": 500}, format="json").status_code == 400
    res = client_for(admin).patch(f"{API}/admin/settings/", {"recognition_enabled": True, "edit_window_minutes": 60}, format="json")
    assert res.json()["recognition_enabled"] is True and res.json()["edit_window_minutes"] == 60
