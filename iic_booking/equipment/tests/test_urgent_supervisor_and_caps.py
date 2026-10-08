"""Surcharge-based urgent requests need supervisor approval before the OIC; weekly caps per equipment."""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import api_views
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    SlotMaster,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
)
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", lambda *a, **k: 0)
    real_event = api_views.create_booking_event

    def _event(**kwargs):
        kwargs["send_notification"] = False
        return real_event(**kwargs)

    monkeypatch.setattr(api_views, "create_booking_event", _event)
    sent = []
    monkeypatch.setattr(
        "iic_booking.communication.styled_transactional_emails._send",
        lambda to, subject, text, html: sent.append({"to": to, "subject": subject, "text": text, "html": html}),
    )
    monkeypatch.setattr(api_views.CommunicationService, "send_email", lambda *a, **k: None)
    return sent


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _dept():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"URG-{tag}", code=f"UR{tag[:4]}", department_type="internal",
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )


def _equipment(**kwargs):
    defaults = {
        "name": "Urgent EQ",
        "code": f"UR{uuid.uuid4().hex[:5].upper()}",
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


def _student_with_supervisor():
    dept = _dept()
    faculty = UserFactory(user_type=UserType.FACULTY, department=dept, admin_approved=True, name="Prof Supervisor")
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    student = UserFactory(user_type=UserType.STUDENT, department=dept, admin_approved=True, name="Stu Dent")
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    return student, faculty


def _hold_booking(user, eq):
    booking = Booking.objects.create(
        user=user,
        equipment=eq,
        charge_profile=ChargeProfile.objects.filter(equipment=eq).first(),
        status=BookingStatus.HOLD,
        total_charge=Decimal("150.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{eq.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )
    start = timezone.now() + timedelta(days=2)
    master = SlotMaster.objects.create(
        equipment=eq, slot_number=1,
        open_time=timezone.localtime(start).time().replace(microsecond=0),
        close_time=timezone.localtime(start + timedelta(hours=1)).time().replace(microsecond=0),
        is_active=True,
    )
    DailySlot.objects.create(
        slot_master=master, date=timezone.localtime(start).date(), start_datetime=start,
        end_datetime=start + timedelta(hours=1), status="HOLD", booking=booking,
    )
    return booking


def _type_b(student, supervisor, eq, *, hold=None, **fields):
    return UrgentBookingRequest.objects.create(
        user=student,
        equipment=eq,
        request_type=UrgentBookingRequestType.REVIEWER_URGENT,
        disclaimer_accepted=True,
        reviewer_comment="Reviewer asked for more data urgently.",
        supervisor=supervisor,
        supervisor_approval_required=supervisor is not None,
        hold_booking=hold,
        **fields,
    )


@pytest.fixture
def rbac_allow():
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        yield


# ---------------------------------------------------------------- weekly caps


def test_weekly_cap_counts_approved_this_week_and_optionally_pending():
    student, faculty = _student_with_supervisor()
    eq = _equipment(max_surcharge_urgent_requests_per_week=1, max_rush_relief_requests_per_week=2)
    rt = UrgentBookingRequestType.REVIEWER_URGENT

    assert api_views._urgent_weekly_cap_error(eq, rt, include_pending=True) is None
    _type_b(student, faculty, eq)
    assert api_views._urgent_weekly_cap_error(eq, rt, include_pending=False) is None
    assert "weekly limit of 1" in api_views._urgent_weekly_cap_error(eq, rt, include_pending=True)

    UrgentBookingRequest.objects.update(status=UrgentBookingRequestStatus.APPROVED, decided_at=timezone.now())
    assert api_views._urgent_weekly_cap_error(eq, rt, include_pending=False)
    # Rush relief has its own cap.
    assert api_views._urgent_weekly_cap_error(eq, UrgentBookingRequestType.NO_SLOT, include_pending=True) is None

    start, _ = api_views._current_week_bounds()
    UrgentBookingRequest.objects.update(decided_at=start - timedelta(minutes=1))
    assert api_views._urgent_weekly_cap_error(eq, rt, include_pending=False) is None


def test_no_cap_when_field_empty():
    student, faculty = _student_with_supervisor()
    eq = _equipment()
    for _ in range(3):
        _type_b(student, faculty, eq, status=UrgentBookingRequestStatus.APPROVED, decided_at=timezone.now())
    assert api_views._urgent_weekly_cap_usage(eq, UrgentBookingRequestType.REVIEWER_URGENT, include_pending=True) == (None, 0)


def test_oic_approval_blocked_when_weekly_cap_reached(rbac_allow):
    student, faculty = _student_with_supervisor()
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    eq = _equipment(max_surcharge_urgent_requests_per_week=1)
    other_student, _ = _student_with_supervisor()
    _type_b(other_student, None, eq, status=UrgentBookingRequestStatus.APPROVED, decided_at=timezone.now())
    urg = _type_b(student, faculty, eq, supervisor_decision="APPROVED")

    res = _client(admin).patch(f"/api/urgent-booking-requests/{urg.id}/", {"status": "APPROVED"}, format="json")
    assert res.status_code == 400
    assert res.json()["code"] == "URGENT_WEEKLY_CAP_REACHED"
    urg.refresh_from_db()
    assert urg.status == UrgentBookingRequestStatus.PENDING


# ---------------------------------------------------------------- supervisor step


def test_oic_cannot_approve_until_supervisor_approves(rbac_allow):
    student, faculty = _student_with_supervisor()
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    eq = _equipment()
    urg = _type_b(student, faculty, eq)
    assert urg.pending_supervisor_approval

    res = _client(admin).patch(f"/api/urgent-booking-requests/{urg.id}/", {"status": "APPROVED"}, format="json")
    assert res.status_code == 400
    assert res.json()["code"] == "SUPERVISOR_APPROVAL_PENDING"

    detail = _client(admin).get(f"/api/urgent-booking-requests/{urg.id}/detail/").json()
    assert detail["pending_wallet_approval"] is True
    assert detail["supervisor_name"] == "Prof Supervisor"


def test_supervisor_approve_forwards_to_oic_without_debit():
    student, faculty = _student_with_supervisor()
    eq = _equipment()
    hold = _hold_booking(student, eq)
    urg = _type_b(student, faculty, eq, hold=hold)

    res = _client(faculty).post(
        f"/api/urgent-booking-requests/{urg.id}/wallet-approve/",
        {"action": "APPROVE", "wallet_notes": "Genuine need"},
        format="json",
    )
    assert res.status_code == 200, res.content
    urg.refresh_from_db()
    hold.refresh_from_db()
    assert urg.supervisor_decision == "APPROVED"
    assert urg.status == UrgentBookingRequestStatus.PENDING
    assert urg.wallet_approved_by_id == faculty.id
    assert urg.wallet_notes == "Genuine need"
    assert not urg.pending_supervisor_approval
    assert hold.status == BookingStatus.HOLD

    again = _client(faculty).post(
        f"/api/urgent-booking-requests/{urg.id}/wallet-approve/", {"action": "REJECT"}, format="json"
    )
    assert again.status_code == 400


def test_supervisor_reject_closes_request_and_releases_hold():
    student, faculty = _student_with_supervisor()
    eq = _equipment()
    hold = _hold_booking(student, eq)
    urg = _type_b(student, faculty, eq, hold=hold)

    res = _client(faculty).post(
        f"/api/urgent-booking-requests/{urg.id}/wallet-approve/", {"action": "REJECT"}, format="json"
    )
    assert res.status_code == 200, res.content
    urg.refresh_from_db()
    hold.refresh_from_db()
    assert urg.status == UrgentBookingRequestStatus.REJECTED
    assert urg.supervisor_decision == "REJECTED"
    assert hold.status == BookingStatus.CANCELLED


def test_only_the_supervisor_can_decide():
    student, faculty = _student_with_supervisor()
    _, other_faculty = _student_with_supervisor()
    urg = _type_b(student, faculty, _equipment())
    for user in (other_faculty, student):
        res = _client(user).post(
            f"/api/urgent-booking-requests/{urg.id}/wallet-approve/", {"action": "APPROVE"}, format="json"
        )
        assert res.status_code == 403
    urg.refresh_from_db()
    assert urg.pending_supervisor_approval


def test_supervisor_lists_and_detail_access():
    student, faculty = _student_with_supervisor()
    eq = _equipment()
    pending = _type_b(student, faculty, eq)
    _type_b(student, faculty, eq, supervisor_decision="APPROVED")
    _type_b(student, None, eq)

    c = _client(faculty)
    rows = c.get("/api/urgent-booking-requests/wallet-pending/").json()["urgent_requests"]
    assert [r["id"] for r in rows] == [pending.id]
    everything = c.get("/api/urgent-booking-requests/wallet/").json()
    assert everything["total_count"] == 2
    approved = c.get("/api/urgent-booking-requests/wallet/?status=approved").json()["urgent_requests"]
    assert len(approved) == 1 and approved[0]["wallet_status"] == "approved"

    assert c.get(f"/api/urgent-booking-requests/{pending.id}/detail/").status_code == 200
    _, stranger = _student_with_supervisor()
    assert _client(stranger).get(f"/api/urgent-booking-requests/{pending.id}/detail/").status_code == 403


def test_pending_actions_route_request_to_supervisor_not_oic(rbac_allow):
    student, faculty = _student_with_supervisor()
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    eq = _equipment()
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    urg = _type_b(student, faculty, eq)

    sup_items = {i["key"]: i for i in collect_pending_actions(faculty)}
    assert sup_items["urgent_requests_supervisor"]["count"] == 1
    assert "urgent_requests" not in {i["key"] for i in collect_pending_actions(oic)}

    urg.supervisor_decision = "APPROVED"
    urg.save(update_fields=["supervisor_decision"])
    assert {i["key"]: i for i in collect_pending_actions(oic)}["urgent_requests"]["count"] == 1
    assert "urgent_requests_supervisor" not in {i["key"] for i in collect_pending_actions(faculty)}


# ---------------------------------------------------------------- signed email links


def test_email_link_get_confirms_then_post_approves(_quiet):
    from iic_booking.communication.styled_transactional_emails import send_urgent_supervisor_action_email

    student, faculty = _student_with_supervisor()
    urg = _type_b(student, faculty, _equipment())
    send_urgent_supervisor_action_email(urg)
    assert _quiet and _quiet[-1]["to"] == faculty.email
    approve_url = re.search(r"Approve: (\S+)", _quiet[-1]["text"]).group(1)
    path = approve_url[approve_url.index("/api/"):]

    anon = APIClient()
    page = anon.get(path)
    assert page.status_code == 200
    assert b"<form" in page.content
    urg.refresh_from_db()
    assert urg.pending_supervisor_approval

    done = anon.post(path, {"notes": "ok"})
    assert done.status_code == 200, done.content
    urg.refresh_from_db()
    assert urg.supervisor_decision == "APPROVED"
    assert urg.wallet_approved_by_id == faculty.id

    assert b"Already processed" in anon.get(path).content


def test_email_link_rejects_tampered_token():
    student, faculty = _student_with_supervisor()
    urg = _type_b(student, faculty, _equipment())
    res = APIClient().post(
        f"/api/urgent-booking-requests/{urg.id}/supervisor-email-action/approve/?token=bogus"
    )
    assert res.status_code == 400
    urg.refresh_from_db()
    assert urg.pending_supervisor_approval


# ---------------------------------------------------------------- create endpoint


@pytest.fixture
def create_unlocked():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked",
        return_value=(False, ""),
    ):
        yield


def test_create_type_b_routes_student_to_supervisor(create_unlocked, _quiet):
    student, faculty = _student_with_supervisor()
    eq = _equipment()
    res = _client(student).post(
        "/api/urgent-booking-requests/create/",
        {
            "equipment_id": eq.pk,
            "request_type": "REVIEWER_URGENT",
            "disclaimer_accepted": True,
            "reviewer_comment": "Reviewer asked for revised spectra within a week.",
            "input_values": {"A": 1},
        },
        format="json",
    )
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["pending_wallet_approval"] is True
    urg = UrgentBookingRequest.objects.get(pk=body["id"])
    assert urg.supervisor_id == faculty.id
    assert urg.supervisor_approval_required
    assert any(m["to"] == faculty.email for m in _quiet)


def test_create_type_b_by_faculty_skips_supervisor(create_unlocked):
    dept = _dept()
    faculty = UserFactory(user_type=UserType.FACULTY, department=dept, admin_approved=True)
    Wallet.objects.get_or_create(user=faculty)
    eq = _equipment()
    res = _client(faculty).post(
        "/api/urgent-booking-requests/create/",
        {
            "equipment_id": eq.pk,
            "request_type": "REVIEWER_URGENT",
            "disclaimer_accepted": True,
            "reviewer_comment": "Reviewer asked for revised spectra within a week.",
            "input_values": {"A": 1},
        },
        format="json",
    )
    assert res.status_code == 201, res.content
    urg = UrgentBookingRequest.objects.get(pk=res.json()["id"])
    assert not urg.supervisor_approval_required
    assert urg.supervisor_id is None
    assert res.json()["pending_wallet_approval"] is False


def test_create_blocked_by_weekly_cap(create_unlocked):
    student, faculty = _student_with_supervisor()
    other, other_sup = _student_with_supervisor()
    eq = _equipment(max_surcharge_urgent_requests_per_week=1)
    _type_b(other, other_sup, eq)
    res = _client(student).post(
        "/api/urgent-booking-requests/create/",
        {
            "equipment_id": eq.pk,
            "request_type": "REVIEWER_URGENT",
            "disclaimer_accepted": True,
            "reviewer_comment": "Reviewer asked for revised spectra within a week.",
            "input_values": {"A": 1},
        },
        format="json",
    )
    assert res.status_code == 400
    assert res.json()["code"] == "URGENT_WEEKLY_CAP_REACHED"
