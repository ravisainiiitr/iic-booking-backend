"""A base whose other mode runs exclusively today is listed and searchable, marked not bookable; booking stays refused."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import mode_utils, search_index
from iic_booking.equipment.models import (
    EquipmentCategory,
    EquipmentModeSchedule,
    ModeScheduleBehavior,
)
from iic_booking.users.models.user_group import UserGroup
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

MONDAY = date(2030, 1, 7)
assert MONDAY.weekday() == 0


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _exclusive(base, mode, start, end):
    return EquipmentModeSchedule.objects.create(
        parent_equipment=base,
        mode_equipment=mode,
        start_date=start,
        end_date=end,
        behavior=ModeScheduleBehavior.EXCLUSIVE,
        weekdays=[],
    )


@pytest.fixture
def nmr(egs_factory):
    f = egs_factory
    category = EquipmentCategory.objects.create(name="NMR spectroscopy", code=f"NMR{uuid.uuid4().hex[:4]}")
    base = f.equipment(name="Nuclear Magnetic Resonance Spectrometer", code="ZNMR", enable_multi_mode=True,
                       category=category)
    txi = f.equipment(name="NMR TXI probe", code="ZNMRTXI", parent_equipment=base, category=category)
    cosy = f.equipment(name="NMR COSY mode", code="ZNMRCOSY", parent_equipment=base, category=category)
    plain = f.equipment(name="Benchtop NMR", code="ZBNMR", category=category)
    today = timezone.localdate()
    _exclusive(base, txi, today, today + timedelta(days=20))
    search_index.invalidate()
    return {"f": f, "base": base, "txi": txi, "cosy": cosy, "plain": plain}


def _rows(client):
    res = client.get("/api/equipments/")
    assert res.status_code == 200
    return {row["equipment_id"]: row for row in res.json()["equipments"]}


def test_helper_reports_running_mode_and_next_working_days(nmr):
    base, txi = nmr["base"], nmr["txi"]
    EquipmentModeSchedule.objects.filter(parent_equipment=base).delete()
    _exclusive(base, txi, MONDAY, MONDAY + timedelta(days=2))
    info = mode_utils.inactive_exclusive_bases([base.pk, txi.pk, nmr["plain"].pk], on_date=MONDAY)
    assert set(info) == {base.pk}
    entry = info[base.pk]
    assert entry["label"] == "Mode not active"
    assert entry["active_mode"] == {"equipment_id": txi.pk, "code": "ZNMRTXI", "name": "NMR TXI probe"}
    assert entry["message"] == "Currently running as NMR TXI probe; booking opens when this mode is active."
    # Thu, Fri, then Mon (weekend skipped).
    assert entry["next_active_dates"] == ["2030-01-10", "2030-01-11", "2030-01-14"]
    assert mode_utils.inactive_exclusive_bases([base.pk], on_date=MONDAY + timedelta(days=3)) == {}


def test_helper_ignores_families_with_multi_mode_off_and_staff(nmr):
    base = nmr["base"]
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    assert mode_utils.inactive_exclusive_bases([base.pk], admin) == {}
    base.enable_multi_mode = False
    base.save(update_fields=["enable_multi_mode"])
    assert mode_utils.inactive_exclusive_bases([base.pk]) == {}


def test_catalogue_lists_inactive_base_marked_not_bookable(nmr):
    rows = _rows(APIClient())
    base, txi = nmr["base"], nmr["txi"]
    assert base.pk in rows, "inactive exclusive base must stay in the catalogue"
    assert rows[base.pk]["mode_inactive"]["active_mode"]["code"] == "ZNMRTXI"
    assert rows[base.pk]["has_child_modes"] is True
    assert rows[txi.pk]["mode_inactive"] is None
    assert rows[nmr["plain"].pk]["mode_inactive"] is None

    student = nmr["f"].student()
    rows = _rows(nmr["f"].client_for(student))
    assert rows[base.pk]["mode_inactive"]["label"] == "Mode not active"

    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    rows = _rows(nmr["f"].client_for(admin))
    assert rows[base.pk]["mode_inactive"] is None


def test_other_hiding_rules_still_apply(nmr):
    base = nmr["base"]
    base.visibility_group = UserGroup.objects.create(name="NMR group", code=f"NMRG{uuid.uuid4().hex[:4]}")
    base.save(update_fields=["visibility_group"])
    assert base.pk not in _rows(APIClient())
    assert APIClient().get(f"/api/equipments/{base.pk}/").status_code == 404

    base.visibility_group = None
    base.visible_to_test_accounts_only = True
    base.save(update_fields=["visibility_group", "visible_to_test_accounts_only"])
    assert base.pk not in _rows(nmr["f"].client_for(nmr["f"].student()))


def test_detail_opens_read_only_but_slots_and_booking_are_refused(nmr):
    base = nmr["base"]
    anon = APIClient()
    res = anon.get(f"/api/equipments/{base.pk}/")
    assert res.status_code == 200
    assert res.json()["mode_inactive"]["active_mode"]["name"] == "NMR TXI probe"
    assert anon.get(f"/api/equipments/{base.pk}/ratings/").status_code == 200
    assert anon.get(f"/api/equipments/{base.pk}/mode-availability/").status_code == 200

    today = timezone.localdate().isoformat()
    assert anon.get(f"/api/equipments/{base.pk}/slots/", {"start_date": today, "end_date": today}).status_code == 404

    student = nmr["f"].student()
    client = nmr["f"].client_for(student)
    assert client.get(f"/api/equipments/{base.pk}/").json()["mode_inactive"] is not None
    assert client.get(f"/api/equipments/{base.pk}/slots/", {"start_date": today, "end_date": today}).status_code == 403
    start = nmr["f"].future(days=1)
    res = client.post(
        f"/api/equipments/{base.pk}/book/",
        {"start_time": start.isoformat(), "end_time": (start + timedelta(hours=1)).isoformat(), "input_values": {}},
        format="json",
    )
    assert res.status_code == 403
    assert res.json()["code"] == "equipment_forbidden"

    # Modes and plain equipment are unaffected.
    assert client.get(f"/api/equipments/{nmr['txi'].pk}/").json()["mode_inactive"] is None


def test_global_search_finds_inactive_base_with_notice(nmr):
    res = APIClient().get("/api/search/", {"q": "ZNMR"})
    assert res.status_code == 200
    hits = {e["code"]: e for e in res.json()["equipment"]}
    assert "ZNMR" in hits
    assert hits["ZNMR"]["mode_inactive"]["active_mode"]["code"] == "ZNMRTXI"
    assert hits["ZNMRTXI"]["mode_inactive"] is None


def test_department_counts_include_inactive_base(nmr):
    res = APIClient().get("/api/equipments/catalog-departments/")
    assert res.status_code == 200
    rows = res.json()["departments"]
    dept = next(r for r in rows if r["id"] == nmr["f"].department.pk)
    assert dept["equipment_count"] == 4


def test_similar_instruments_skip_inactive_modes(nmr):
    f = nmr["f"]
    tomorrow = f.future(days=1)
    for eq in (nmr["base"], nmr["txi"], nmr["cosy"]):
        f.slot(eq, tomorrow)
    student = f.student()
    body = f.client_for(student).get(f"/api/equipments/{nmr['plain'].pk}/similar-availability/").json()
    names = [row["name"] for row in body["alternatives"]]
    assert "NMR TXI probe" in names
    assert "Nuclear Magnetic Resonance Spectrometer" not in names
    assert "NMR COSY mode" not in names
