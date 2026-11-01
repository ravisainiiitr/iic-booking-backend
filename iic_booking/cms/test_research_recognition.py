"""Hall of Frames and Researcher of the Month, worked out from approved publications; the Main Administrator can take
anyone off the home page (``cms.recognition``)."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.cms import recognition
from iic_booking.cms.models import RecognitionHide, ResearcherOfTheMonth
from iic_booking.equipment.models import Equipment, EquipmentPublicationClaim, EquipmentPublicationClaimStatus
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
THIS_MONTH = timezone.localdate().replace(day=1)


def _dept(name):
    return Department.objects.create(name=name, code=f"D{uuid.uuid4().hex[:5].upper()}",
                                     department_type=DepartmentType.INTERNAL)


def _equipment(name="FE-SEM"):
    return Equipment.objects.create(name=name, code=f"E{uuid.uuid4().hex[:5].upper()}", slot_duration_minutes=60,
                                    user_rating_enabled=False)


def _researcher(name, dept, **extra):
    return UserFactory(name=name, user_type=UserType.STUDENT, department=dept, admin_approved=True,
                       email_verified=True, **extra)


def _approved(user, *, days_ago=10, title="Paper", journal="Nature Materials", year=2026, doi="", equipment=None,
              status=EquipmentPublicationClaimStatus.APPROVED):
    claim = EquipmentPublicationClaim.objects.create(
        submitted_by=user, title=title, journal=journal, year=year, doi=doi, status=status,
        reviewed_at=timezone.make_aware(datetime.combine(THIS_MONTH, time(9))) - timedelta(days=days_ago),
    )
    if equipment is not None:
        claim.equipments.add(equipment)
    return claim


def _client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user)
    return c


def _main_admin():
    return UserFactory(user_type=UserType.ADMIN, email_verified=True, admin_approved=True)


# --- Hall of Frames -------------------------------------------------------------------------------------------------

def test_hall_of_frames_ranks_researchers_by_approved_publications():
    chem = _dept("Chemistry")
    sem = _equipment("FE-SEM")
    sachin = _researcher("Sachin Kumar Shrivastava", chem)
    other = _researcher("Asha Verma", chem)
    for i in range(8):
        _approved(sachin, days_ago=20 + i, title=f"Paper {i}")
    _approved(sachin, days_ago=1, title="Newest work", doi="10.1/abc", equipment=sem)
    _approved(other, days_ago=5)
    _approved(other, status=EquipmentPublicationClaimStatus.PENDING)  # not counted until approved
    _approved(other, status=EquipmentPublicationClaimStatus.REJECTED)

    rows = _client().get("/api/cms/hall-of-frames/").json()["results"]
    assert [(r["researcher_name"], r["publication_count"]) for r in rows] == [
        ("Sachin Kumar Shrivastava", 9), ("Asha Verma", 1)]
    top = rows[0]
    assert (top["publication_title"], top["publication_url"], top["facility"], top["affiliation"]) == (
        "Newest work", "https://doi.org/10.1/abc", "FE-SEM", "Chemistry")


def test_inactive_researchers_are_left_out():
    gone = _researcher("Gone User", _dept("Physics"))
    _approved(gone)
    type(gone).objects.filter(pk=gone.pk).update(is_active=False)
    assert _client().get("/api/cms/hall-of-frames/").json()["results"] == []


def test_main_admin_hides_and_shows_a_researcher_in_the_hall():
    r = _researcher("Hidden Person", _dept("Civil"))
    _approved(r)
    admin = _main_admin()
    assert _client(admin).post(f"/api/cms/recognition/hall/{r.pk}/", {"hidden": True, "reason": "requested"},
                               format="json").status_code == 200
    assert _client().get("/api/cms/hall-of-frames/").json()["results"] == []
    listed = _client(admin).get("/api/cms/recognition/").json()["hall"]
    assert listed[0]["user_id"] == r.pk and listed[0]["hidden"] is True
    hide = RecognitionHide.objects.get(user=r)
    assert hide.created_by == admin and hide.reason == "requested"
    _client(admin).post(f"/api/cms/recognition/hall/{r.pk}/", {"hidden": False}, format="json")
    assert len(_client().get("/api/cms/hall-of-frames/").json()["results"]) == 1


def test_only_the_main_admin_can_take_anyone_off(monkeypatch):
    r = _researcher("Someone", _dept("Maths"))
    _approved(r)
    dept_admin = UserFactory(user_type=UserType.DEPT_ADMIN)
    assert _client(dept_admin).post(f"/api/cms/recognition/hall/{r.pk}/", {"hidden": True}, format="json").status_code == 403
    student = UserFactory(user_type=UserType.STUDENT)
    assert _client(student).get("/api/cms/recognition/").status_code == 403
    assert _client().post(f"/api/cms/recognition/hall/{r.pk}/", {"hidden": True}, format="json").status_code in (401, 403)


# --- Researcher of the Month ----------------------------------------------------------------------------------------

def test_chosen_automatically_per_department_and_institute_wide():
    chem, phy = _dept("Chemistry"), _dept("Physics")
    a, b, c = _researcher("Chem Top", chem), _researcher("Chem Second", chem), _researcher("Phy Top", phy)
    for _ in range(3):
        _approved(a)
    _approved(b)
    _approved(c, days_ago=2)
    _approved(c, days_ago=3)
    data = _client().get("/api/cms/researcher-of-the-month/").json()
    got = {r["department_name"]: (r["researcher_name"], r["publication_count"]) for r in data["results"]}
    assert got == {"Institute-wide": ("Chem Top", 3), "Chemistry": ("Chem Top", 3), "Physics": ("Phy Top", 2)}
    # Stored once: a second visit chooses nothing new.
    _client().get("/api/cms/researcher-of-the-month/")
    assert ResearcherOfTheMonth.objects.filter(month=THIS_MONTH).count() == 3


def test_only_the_last_twelve_months_count():
    d = _dept("Chemistry")
    old, recent = _researcher("Old Star", d), _researcher("Recent Author", d)
    for _ in range(5):
        _approved(old, days_ago=400)
    _approved(recent)
    rows = _client().get("/api/cms/researcher-of-the-month/").json()["results"]
    assert {r["researcher_name"] for r in rows} == {"Recent Author"}


def test_it_rotates_but_repeats_when_nobody_else_qualifies():
    d = _dept("Chemistry")
    top, next_ = _researcher("Top", d), _researcher("Next", d)
    for _ in range(3):
        _approved(top, days_ago=40)
    _approved(next_, days_ago=40)
    last = (THIS_MONTH - timedelta(days=1)).replace(day=1)
    ResearcherOfTheMonth.objects.create(month=last, department=d, user=top, auto=True, is_approved=True,
                                        researcher_name="Top")
    assert recognition.choose(THIS_MONTH, d.pk).user_id == next_.pk  # featured last month: passed over
    alone = _dept("Solo")
    only = _researcher("Only One", alone)
    _approved(only, days_ago=40)
    ResearcherOfTheMonth.objects.create(month=last, department=alone, user=only, auto=True, is_approved=True,
                                        researcher_name="Only One")
    assert recognition.choose(THIS_MONTH, alone.pk).user_id == only.pk  # nobody else: repeats


def test_disabling_this_months_researcher_picks_the_next_one_and_keeps_a_record():
    d = _dept("Chemistry")
    top, next_ = _researcher("Top", d), _researcher("Next", d)
    for _ in range(3):
        _approved(top)
    _approved(next_)
    _client().get("/api/cms/researcher-of-the-month/")
    row = ResearcherOfTheMonth.objects.get(month=THIS_MONTH, department=d)
    admin = _main_admin()
    res = _client(admin).post(f"/api/cms/recognition/month/{row.pk}/disable/", {"reason": "duplicate"}, format="json")
    assert res.status_code == 200 and res.json()["researcher_name"] == "Next"
    assert RecognitionHide.objects.filter(scope="month", user=top, month=THIS_MONTH, department=d).exists()
    # Nobody else left: the card is hidden.
    row.refresh_from_db()
    _client(admin).post(f"/api/cms/recognition/month/{row.pk}/disable/", {}, format="json")
    row.refresh_from_db()
    assert row.is_approved is False
    shown = {r["department_name"] for r in _client().get("/api/cms/researcher-of-the-month/").json()["results"]}
    assert "Chemistry" not in shown


def test_earlier_months_stay_as_history():
    d = _dept("Chemistry")
    r = _researcher("Past Winner", d)
    ResearcherOfTheMonth.objects.create(month=date(2025, 5, 1), department=d, user=r, auto=True, is_approved=True,
                                        researcher_name="Past Winner", publication_count=4)
    history = _client().get("/api/cms/researcher-of-the-month/?history=1").json()["results"]
    assert [(h["researcher_name"], h["month"]) for h in history] == [("Past Winner", "2025-05-01")]
