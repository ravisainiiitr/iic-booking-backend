"""Equipment Booking Configuration: waitlist / urgent request depth for OICs; slot window reference is Main Admin only."""

from __future__ import annotations

import logging
import uuid
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import api_views
from iic_booking.equipment.models import (
    ChargeProfile,
    Equipment,
    EquipmentManager,
    EquipmentTemporaryOIC,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
    WaitlistEntry,
)
from iic_booking.equipment.waitlist import add_user_to_waitlist
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

LIST_URL = "/api/oic/equipment-settings/"
CREATE_URGENT_URL = "/api/urgent-booking-requests/create/"
AUDIT_LOGGER = "iic_booking.audit.staff_actions"


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", lambda *a, **k: 0)
    monkeypatch.setattr(api_views.CommunicationService, "send_email", lambda *a, **k: None)
    monkeypatch.setattr(
        "iic_booking.communication.styled_transactional_emails._send", lambda *a, **k: None
    )


@pytest.fixture
def create_unlocked():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked",
        return_value=(False, ""),
    ):
        yield


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


def _dept():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"DEP-{tag}", code=f"DP{tag[:4]}", department_type="internal",
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )


def _equipment(**kwargs):
    defaults = {
        "name": f"Depth EQ {uuid.uuid4().hex[:4]}",
        "code": f"DE{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
    }
    defaults.update(kwargs)
    eq = Equipment.objects.create(**defaults)
    for user_type in (UserType.STUDENT, UserType.FACULTY):
        ChargeProfile.objects.create(
            equipment=eq, user_type=user_type, profile_type="SAMPLE", primary_unit_charge=Decimal("100.00")
        )
    return eq


def _oic_with(eq):
    oic = _user(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic


def _url(eq) -> str:
    return f"/api/oic/equipment-settings/{eq.pk}/"


def _urgent(eq, request_type, status=UrgentBookingRequestStatus.PENDING, **fields):
    return UrgentBookingRequest.objects.create(
        user=_user(user_type=UserType.STUDENT),
        equipment=eq,
        request_type=request_type,
        disclaimer_accepted=True,
        status=status,
        **fields,
    )


# ---------------------------------------------------------------- defaults and editing


def test_defaults_are_todays_behaviour_and_empty_keeps_them():
    eq = _equipment()
    oic = _oic_with(eq)

    row = _client(oic).get(LIST_URL).data["equipments"][0]
    assert row["settings"]["waitlist_queue_depth"] == 0
    assert row["settings"]["max_urgent_requests"] is None
    assert row["settings"]["max_rush_relief_requests_per_week"] is None
    assert row["settings"]["max_surcharge_urgent_requests_per_week"] is None
    assert row["usage"] == {"waitlist_active": 0, "urgent_pending": 0, "rush_relief_this_week": 0, "surcharge_this_week": 0}

    res = _client(oic).patch(
        _url(eq),
        {
            "waitlist_queue_depth": "",
            "max_urgent_requests": None,
            "max_rush_relief_requests_per_week": "",
            "max_surcharge_urgent_requests_per_week": None,
        },
        format="json",
    )
    assert res.status_code == 200, res.data
    eq.refresh_from_db()
    assert eq.waitlist_queue_depth == 0
    assert eq.max_urgent_requests is None
    assert eq.max_rush_relief_requests_per_week is None
    assert eq.max_surcharge_urgent_requests_per_week is None


def test_oic_sets_depths_with_validation_and_audit(caplog):
    eq = _equipment()
    oic = _oic_with(eq)
    body = {
        "waitlist_queue_depth": 10,
        "max_urgent_requests": 4,
        "max_rush_relief_requests_per_week": 2,
        "max_surcharge_urgent_requests_per_week": 0,
    }
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        res = _client(oic).patch(_url(eq), body, format="json")
    assert res.status_code == 200, res.data
    eq.refresh_from_db()
    assert (
        eq.waitlist_queue_depth,
        eq.max_urgent_requests,
        eq.max_rush_relief_requests_per_week,
        eq.max_surcharge_urgent_requests_per_week,
    ) == (10, 4, 2, 0)
    audit = [r.getMessage() for r in caplog.records if r.name == AUDIT_LOGGER]
    assert any(
        "action=equipment_settings.update" in m and "'waitlist_queue_depth': [0, 10]" in m and f"equipment_id={eq.pk}" in m
        for m in audit
    ), audit

    bad = _client(oic).patch(
        _url(eq),
        {"waitlist_queue_depth": 501, "max_urgent_requests": -1, "max_rush_relief_requests_per_week": "2.5",
         "max_surcharge_urgent_requests_per_week": 101},
        format="json",
    )
    assert bad.status_code == 400
    assert set(bad.data["errors"]) == {
        "waitlist_queue_depth", "max_urgent_requests", "max_rush_relief_requests_per_week",
        "max_surcharge_urgent_requests_per_week",
    }
    eq.refresh_from_db()
    assert eq.waitlist_queue_depth == 10


def test_unchanged_legacy_value_above_maximum_does_not_block_other_saves():
    eq = _equipment(waitlist_queue_depth=900, max_urgent_requests=250)
    oic = _oic_with(eq)
    res = _client(oic).patch(
        _url(eq),
        {"waitlist_queue_depth": 900, "max_urgent_requests": 250, "booking_not_utilize_window_hours": 6},
        format="json",
    )
    assert res.status_code == 200, res.data
    eq.refresh_from_db()
    assert (eq.waitlist_queue_depth, eq.max_urgent_requests, eq.booking_not_utilize_window_hours) == (900, 250, 6)


def test_oic_scope_for_depth_settings():
    mine = _equipment()
    oic = _oic_with(mine)
    other = _equipment()
    assert _client(oic).patch(_url(other), {"waitlist_queue_depth": 5}, format="json").status_code == 403
    other.refresh_from_db()
    assert other.waitlist_queue_depth == 0
    assert [r["equipment_id"] for r in _client(oic).get(LIST_URL).data["equipments"]] == [mine.pk]

    temp = _user(user_type=UserType.MANAGER)
    EquipmentTemporaryOIC.objects.create(
        equipment=other, temporary_oic=temp, primary_oic=oic, resume_at=timezone.now() + timedelta(days=2)
    )
    assert _client(temp).patch(_url(other), {"waitlist_queue_depth": 5}, format="json").status_code == 200
    other.refresh_from_db()
    assert other.waitlist_queue_depth == 5

    for user_type in (UserType.STUDENT, UserType.OPERATOR):
        assert _client(_user(user_type=user_type)).patch(_url(mine), {"waitlist_queue_depth": 5}, format="json").status_code == 403

    admin = _user(user_type=UserType.ADMIN)
    assert _client(admin).patch(_url(mine), {"max_urgent_requests": 3}, format="json").status_code == 200


def test_listing_reports_current_fill():
    eq = _equipment(waitlist_queue_depth=10)
    oic = _oic_with(eq)
    for _ in range(3):
        WaitlistEntry.objects.create(equipment=eq, user=_user(user_type=UserType.STUDENT), status="ACTIVE")
    WaitlistEntry.objects.create(equipment=eq, user=_user(user_type=UserType.STUDENT), status="OPT_OUT")
    _urgent(eq, UrgentBookingRequestType.NO_SLOT)
    _urgent(eq, UrgentBookingRequestType.NO_SLOT, status=UrgentBookingRequestStatus.APPROVED, decided_at=timezone.now())
    _urgent(eq, UrgentBookingRequestType.REVIEWER_URGENT)
    start, _ = api_views._current_week_bounds()
    _urgent(eq, UrgentBookingRequestType.REVIEWER_URGENT, status=UrgentBookingRequestStatus.APPROVED,
            decided_at=start - timedelta(hours=1))

    usage = _client(oic).get(LIST_URL).data["equipments"][0]["usage"]
    assert usage == {"waitlist_active": 3, "urgent_pending": 2, "rush_relief_this_week": 2, "surcharge_this_week": 1}


# ---------------------------------------------------------------- slot window reference: Main Admin only


def test_oic_cannot_change_slot_window_reference():
    eq = _equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    oic = _oic_with(eq)

    row = _client(oic).get(LIST_URL).data
    assert row["can_edit_slot_window_reference"] is False

    for body in (
        {"slot_window_reference_weekday": 4},
        {"slot_window_reference_time": "09:00"},
        {"slot_window_reference_weekday": None},
        {"slot_window_reference_weekday": 4, "weekly_view_time_from": "08:00"},
    ):
        res = _client(oic).patch(_url(eq), body, format="json")
        assert res.status_code == 400, body
        assert set(res.data["errors"]) & {"slot_window_reference_weekday", "slot_window_reference_time"}
    eq.refresh_from_db()
    assert (eq.slot_window_reference_weekday, eq.slot_window_reference_time, eq.weekly_view_time_from) == (2, time(21, 0), None)

    same = _client(oic).patch(
        _url(eq),
        {"slot_window_reference_weekday": 2, "slot_window_reference_time": "21:00", "weekly_view_time_from": "08:00"},
        format="json",
    )
    assert same.status_code == 200, same.data
    eq.refresh_from_db()
    assert eq.weekly_view_time_from == time(8, 0)
    assert (eq.slot_window_reference_weekday, eq.slot_window_reference_time) == (2, time(21, 0))


def test_main_admin_can_change_slot_window_reference():
    eq = _equipment()
    admin = _user(user_type=UserType.ADMIN)
    assert _client(admin).get(LIST_URL).data["can_edit_slot_window_reference"] is True
    res = _client(admin).patch(
        _url(eq), {"slot_window_reference_weekday": 3, "slot_window_reference_time": "18:30"}, format="json"
    )
    assert res.status_code == 200, res.data
    eq.refresh_from_db()
    assert (eq.slot_window_reference_weekday, eq.slot_window_reference_time) == (3, time(18, 30))


# ---------------------------------------------------------------- enforcement: waitlist


def test_full_waitlist_refuses_new_users_and_flags_it():
    eq = _equipment(name="FESEM", waitlist_queue_depth=2)
    first, second, third = (_user(user_type=UserType.STUDENT) for _ in range(3))
    assert add_user_to_waitlist(eq, first) == (True, 1)
    WaitlistEntry.objects.create(equipment=eq, user=_user(user_type=UserType.STUDENT), status="OPT_OUT")
    assert add_user_to_waitlist(eq, second) == (True, 2)
    assert add_user_to_waitlist(eq, third) == (False, None)
    assert not WaitlistEntry.objects.filter(equipment=eq, user=third).exists()

    payload = api_views._enrich_failed_booking_response(eq, third, "Slot already booked.", True)
    assert payload == {"error": "Slot already booked.", "waitlist_full": True}


def test_waitlist_depth_zero_keeps_waitlist_off():
    eq = _equipment(waitlist_queue_depth=0)
    assert add_user_to_waitlist(eq, _user(user_type=UserType.STUDENT)) == (False, None)
    payload = api_views._enrich_failed_booking_response(eq, _user(user_type=UserType.STUDENT), "Slot taken.", True)
    assert payload == {"error": "Slot taken."}


# ---------------------------------------------------------------- enforcement: urgent requests


def _submit(user, eq, request_type):
    body = {"equipment_id": eq.pk, "request_type": request_type, "disclaimer_accepted": True}
    if request_type == "REVIEWER_URGENT":
        body["reviewer_comment"] = "Reviewer asked for revised spectra within a week."
        body["input_values"] = {"A": 1}
    return _client(user).post(CREATE_URGENT_URL, body, format="json")


def test_open_urgent_limit_refuses_both_types(create_unlocked):
    eq = _equipment(name="XRD", max_urgent_requests=1)
    _urgent(eq, UrgentBookingRequestType.REVIEWER_URGENT)
    faculty = _user(user_type=UserType.FACULTY, department=_dept())
    for request_type in ("NO_SLOT", "REVIEWER_URGENT"):
        res = _submit(faculty, eq, request_type)
        assert res.status_code == 400
        assert res.json()["code"] == "URGENT_OPEN_LIMIT_REACHED"
        assert "XRD already has the maximum of 1 open urgent request" in res.json()["error"]


def test_type_a_weekly_limit_refuses_and_type_b_unaffected(create_unlocked):
    eq = _equipment(max_rush_relief_requests_per_week=1)
    _urgent(eq, UrgentBookingRequestType.NO_SLOT)
    faculty = _user(user_type=UserType.FACULTY, department=_dept())

    res = _submit(faculty, eq, "NO_SLOT")
    assert res.status_code == 400
    assert res.json()["code"] == "URGENT_WEEKLY_CAP_REACHED"
    assert "rush-relief" in res.json()["error"]

    res_b = _submit(faculty, eq, "REVIEWER_URGENT")
    assert res_b.status_code == 201, res_b.content


def test_type_b_limit_zero_refuses_all_and_empty_is_uncapped(create_unlocked):
    eq = _equipment(max_surcharge_urgent_requests_per_week=0)
    faculty = _user(user_type=UserType.FACULTY, department=_dept())
    res = _submit(faculty, eq, "REVIEWER_URGENT")
    assert res.status_code == 400
    assert res.json()["code"] == "URGENT_WEEKLY_CAP_REACHED"

    uncapped = _equipment()
    for _ in range(5):
        _urgent(uncapped, UrgentBookingRequestType.REVIEWER_URGENT)
    assert _submit(faculty, uncapped, "REVIEWER_URGENT").status_code == 201
