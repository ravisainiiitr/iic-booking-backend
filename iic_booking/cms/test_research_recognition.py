"""Hall of Frames and Researcher of the Month (Module 5): only approved entries are public, only the Main
Administrator approves, the current month replaces the previous one, history is kept, and Department Administrators
feature researchers of their own department only."""

from __future__ import annotations

from datetime import date, timedelta
from io import BytesIO

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.cms.models import HallOfFrameEntry, ResearcherOfTheMonth
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

HOF_ADMIN = "/api/admin/cms-hall-of-frames/"
ROTM_ADMIN = "/api/admin/cms-researchers-of-the-month/"


def _dept(code):
    return Department.objects.create(name=f"Dept {code}", code=code, department_type=DepartmentType.INTERNAL)


def _client(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user)
    return c


def _main_admin():
    return UserFactory(user_type=UserType.ADMIN, email_verified=True, admin_approved=True)


def _dept_admin(dept, monkeypatch):
    u = UserFactory(user_type=UserType.DEPT_ADMIN, department=dept, email_verified=True, admin_approved=True)
    monkeypatch.setattr("iic_booking.users.rbac.user_has_admin_panel_access", lambda user: True)
    return u


def _png(size=(1600, 900)):
    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, (20, 120, 200)).save(buf, format="PNG")
    return SimpleUploadedFile("portrait.png", buf.getvalue(), content_type="image/png")


def _this_month():
    return timezone.localdate().replace(day=1)


# --- Hall of Frames -----------------------------------------------------------------------------------------------

def test_only_approved_hall_of_frames_entries_are_public_in_order():
    HallOfFrameEntry.objects.create(researcher_name="Draft Person", is_approved=False)
    HallOfFrameEntry.objects.create(researcher_name="Second", is_approved=True, order=2, year=2025)
    HallOfFrameEntry.objects.create(researcher_name="First", is_approved=True, order=1, journal="Nature Materials",
                                    facility="IIC FE-SEM facility")
    rows = _client().get("/api/cms/hall-of-frames/").json()["results"]
    assert [r["researcher_name"] for r in rows] == ["First", "Second"]
    assert rows[0]["journal"] == "Nature Materials" and rows[0]["facility"] == "IIC FE-SEM facility"


def test_main_admin_creates_and_approves_with_a_resized_photo(settings, tmp_path):
    settings.STORAGES = {**settings.STORAGES, "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}
    settings.MEDIA_ROOT = str(tmp_path)
    admin = _main_admin()
    res = _client(admin).post(HOF_ADMIN, {"researcher_name": "Dr. Asha", "affiliation": "Chemistry, IIT Roorkee",
                                          "year": 2026, "is_approved": "true", "photo": _png()}, format="multipart")
    assert res.status_code == 201, res.content
    entry = HallOfFrameEntry.objects.get(pk=res.json()["id"])
    assert entry.is_approved and entry.approved_by == admin and entry.approved_at
    from PIL import Image

    with entry.photo.open("rb") as fh:
        img = Image.open(fh)
        assert max(img.size) <= 640 and img.format == "JPEG"


def test_non_image_upload_is_refused():
    res = _client(_main_admin()).post(HOF_ADMIN, {"researcher_name": "X", "photo": SimpleUploadedFile(
        "x.png", b"not an image", content_type="image/png")}, format="multipart")
    assert res.status_code == 400


def test_others_cannot_approve_and_their_edits_return_an_entry_to_draft(monkeypatch):
    dept = _dept("CHM")
    editor = _dept_admin(dept, monkeypatch)
    entry = HallOfFrameEntry.objects.create(researcher_name="Approved", is_approved=True)
    assert _client(editor).post(HOF_ADMIN, {"researcher_name": "Y", "is_approved": True},
                                format="json").status_code == 403
    res = _client(editor).patch(f"{HOF_ADMIN}{entry.pk}/", {"journal": "Edited"}, format="json")
    assert res.status_code == 200
    entry.refresh_from_db()
    assert entry.journal == "Edited" and not entry.is_approved  # back to the Main Administrator for approval


def test_students_and_visitors_cannot_manage_entries():
    student = UserFactory(user_type=UserType.STUDENT)
    assert _client(student).get(HOF_ADMIN).status_code == 403
    assert _client().post(HOF_ADMIN, {"researcher_name": "Z"}, format="json").status_code in (401, 403)


# --- Researcher of the Month --------------------------------------------------------------------------------------

def test_this_months_approved_researchers_replace_last_months_and_history_is_kept():
    chem, phy = _dept("CH2"), _dept("PH2")
    now = _this_month()
    last = (now - timedelta(days=1)).replace(day=1)
    ResearcherOfTheMonth.objects.create(month=last, department=chem, researcher_name="Last Month", is_approved=True)
    ResearcherOfTheMonth.objects.create(month=now, department=chem, researcher_name="Chem Now", is_approved=True)
    ResearcherOfTheMonth.objects.create(month=now, department=phy, researcher_name="Phy Draft", is_approved=False)
    ResearcherOfTheMonth.objects.create(month=now.replace(day=15), researcher_name="Institute", is_approved=True)

    current = _client().get("/api/cms/researcher-of-the-month/").json()
    assert current["month"] == now.isoformat()
    assert sorted(r["researcher_name"] for r in current["results"]) == ["Chem Now", "Institute"]
    assert {r["department_name"] for r in current["results"]} == {chem.name, "Institute-wide"}
    only_chem = _client().get(f"/api/cms/researcher-of-the-month/?department={chem.pk}").json()["results"]
    assert [r["researcher_name"] for r in only_chem] == ["Chem Now"]
    history = _client().get("/api/cms/researcher-of-the-month/?history=1").json()["results"]
    assert [r["researcher_name"] for r in history] == ["Last Month"]
    assert ResearcherOfTheMonth.objects.get(researcher_name="Institute").month == now  # stored as the 1st


def test_one_researcher_per_department_per_month():
    dept = _dept("CH3")
    month = date(2026, 12, 1)
    ResearcherOfTheMonth.objects.create(month=month, department=dept, researcher_name="A")
    res = _client(_main_admin()).post(ROTM_ADMIN, {"researcher_name": "B", "month": "2026-12-20",
                                                   "department": dept.pk}, format="json")
    assert res.status_code == 400 and "already has" in str(res.json())
    ResearcherOfTheMonth.objects.create(month=month, researcher_name="Institute A")
    with pytest.raises(IntegrityError), transaction.atomic():
        ResearcherOfTheMonth.objects.create(month=month, researcher_name="Institute B")


def test_scheduling_a_future_month_does_not_show_it_yet():
    future = (_this_month() + timedelta(days=40)).replace(day=1)
    ResearcherOfTheMonth.objects.create(month=future, researcher_name="Next", is_approved=True)
    assert _client().get("/api/cms/researcher-of-the-month/").json()["results"] == []


def test_department_admins_feature_only_their_own_department(monkeypatch):
    own, other = _dept("OWN"), _dept("OTH")
    admin = _dept_admin(own, monkeypatch)
    ResearcherOfTheMonth.objects.create(month=date(2026, 12, 1), department=other, researcher_name="Theirs")
    client = _client(admin)
    assert client.post(ROTM_ADMIN, {"researcher_name": "Mine", "month": "2026-12-01", "department": other.pk},
                       format="json").status_code == 403
    created = client.post(ROTM_ADMIN, {"researcher_name": "Mine", "month": "2026-12-01", "department": own.pk},
                          format="json")
    assert created.status_code == 201 and created.json()["is_approved"] is False
    listed = client.get(ROTM_ADMIN).json()
    listed = listed.get("results", listed) if isinstance(listed, dict) else listed
    assert [r["researcher_name"] for r in listed] == ["Mine"]
    assert ResearcherOfTheMonth.objects.get(researcher_name="Mine").created_by == admin
