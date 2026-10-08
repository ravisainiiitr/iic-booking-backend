"""Type B urgent requests email the OIC(s) on submission and on supervisor approval; sign-in attention items for OICs."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.template import Context
from django.template import Template
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication.models import CommunicationLog
from iic_booking.communication.models import CommunicationTemplate
from iic_booking.communication.service import CommunicationService
from iic_booking.equipment import api_views
from iic_booking.equipment.models import Booking
from iic_booking.equipment.models import BookingEvent
from iic_booking.equipment.models import BookingStatus
from iic_booking.equipment.models import ChargeProfile
from iic_booking.equipment.models import Equipment
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentTemporaryOIC
from iic_booking.equipment.models import UrgentBookingRequest
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.equipment.urgent_oic_alerts import EMAIL_TEMPLATE_CODE
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet
from iic_booking.users.models.wallet import WalletJoinRequest
from iic_booking.users.models.wallet import WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import WalletRepository
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

REASON = "Reviewer asked for revised spectra within a week."


@pytest.fixture(autouse=True)
def emails(monkeypatch):
    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", lambda *a, **k: 0)
    monkeypatch.setattr("iic_booking.communication.styled_transactional_emails._send", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(CommunicationService, "send_email", lambda *a, **k: sent.append(k))
    return sent


@pytest.fixture(autouse=True)
def unlocked():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked",
        return_value=(False, ""),
    ), patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        yield


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _oic():
    return UserFactory(user_type=UserType.MANAGER, admin_approved=True)


def _equipment(dept, name="Alert EQ"):
    eq = Equipment.objects.create(
        name=name, code=f"OA{uuid.uuid4().hex[:5].upper()}", slot_duration_minutes=60,
        user_rating_enabled=False, status="ACTIVE", internal_department=dept,
    )
    ChargeProfile.objects.create(
        equipment=eq, user_type=UserType.STUDENT, profile_type="SAMPLE", time_formula="A*45",
        primary_unit_charge=Decimal("100.00"),
    )
    return eq


def _setup():
    tag = uuid.uuid4().hex[:6].upper()
    dept = Department.objects.create(
        name=f"OIA-{tag}", code=f"OA{tag[:4]}", department_type="internal",
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )
    eq = _equipment(dept)
    faculty = UserFactory(user_type=UserType.FACULTY, department=dept, admin_approved=True)
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    student = UserFactory(user_type=UserType.STUDENT, department=dept, admin_approved=True)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    target, _ = WalletRepository.get_booking_wallet_target(student, dept)
    target.balance = Decimal("1000.00")
    target.save(update_fields=["balance"])
    oic = _oic()
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    temp_oic = _oic()
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=temp_oic, resume_at=timezone.now() + timedelta(days=3)
    )
    ended_temp = _oic()
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=ended_temp, resume_at=timezone.now() - timedelta(days=1)
    )
    other_oic = _oic()
    EquipmentManager.objects.create(equipment=_equipment(dept, "Other EQ"), manager=other_oic)
    return {
        "dept": dept, "eq": eq, "faculty": faculty, "student": student, "oic": oic,
        "temp_oic": temp_oic, "ended_temp": ended_temp, "other_oic": other_oic,
    }


def _submit(student, eq):
    body = {
        "equipment_id": eq.pk,
        "request_type": "REVIEWER_URGENT",
        "disclaimer_accepted": True,
        "reviewer_comment": REASON,
        "input_values": {"A": 2},
        "preferred_schedule": "Any morning next week",
    }
    res = _client(student).post("/api/urgent-booking-requests/create/", body, format="json")
    assert res.status_code == 201, res.content
    return UrgentBookingRequest.objects.get(pk=res.json()["id"])


def _oic_alerts(emails):
    return [e for e in emails if e.get("template") == EMAIL_TEMPLATE_CODE]


def _recipient_ids(alerts):
    return sorted(e["recipient"].id for e in alerts)


# ---------------------------------------------------------------- emails


def test_submit_and_supervisor_approval_email_oic_and_temp_oic_once_each(emails):
    s = _setup()
    urg = _submit(s["student"], s["eq"])

    submitted = _oic_alerts(emails)
    assert _recipient_ids(submitted) == sorted([s["oic"].id, s["temp_oic"].id])
    ctx = submitted[0]["template_context"]
    assert submitted[0]["metadata"] == {"urgent_booking_request_id": urg.id, "oic_alert_stage": "submitted"}
    assert ctx["request_id"] == urg.id
    assert ctx["status_label"] == "Awaiting supervisor approval"
    assert ctx["link"].endswith(f"/urgent-requests?request={urg.id}")
    assert ctx["equipment_name"] == "Alert EQ"
    assert ctx["requester_category"] == str(s["student"].get_user_type_display_label())
    assert ctx["required_time"] and ctx["amount"].startswith("₹")
    assert ctx["preferred_schedule"] == "Any morning next week"
    assert ctx["reason"] == REASON

    emails.clear()
    res = _client(s["faculty"]).post(
        f"/api/urgent-booking-requests/{urg.id}/wallet-approve/", {"action": "APPROVE"}, format="json"
    )
    assert res.status_code == 200, res.content
    approved = _oic_alerts(emails)
    assert _recipient_ids(approved) == sorted([s["oic"].id, s["temp_oic"].id])
    assert approved[0]["template_context"]["status_label"] == "Ready to allocate"
    assert approved[0]["metadata"]["oic_alert_stage"] == "supervisor_approved"
    assert approved[0]["template_context"]["link"].endswith(f"/urgent-requests?request={urg.id}")

    emails.clear()
    _client(s["faculty"]).post(
        f"/api/urgent-booking-requests/{urg.id}/wallet-approve/", {"action": "APPROVE"}, format="json"
    )
    assert _oic_alerts(emails) == []


def test_supervisor_rejection_sends_no_ready_email(emails):
    s = _setup()
    urg = _submit(s["student"], s["eq"])
    emails.clear()
    res = _client(s["faculty"]).post(
        f"/api/urgent-booking-requests/{urg.id}/wallet-approve/", {"action": "REJECT"}, format="json"
    )
    assert res.status_code == 200, res.content
    assert _oic_alerts(emails) == []


def test_request_without_supervisor_step_is_ready_on_submission(emails):
    s = _setup()
    faculty_requester = s["faculty"]
    target, _ = WalletRepository.get_booking_wallet_target(faculty_requester, s["dept"])
    target.balance = Decimal("1000.00")
    target.save(update_fields=["balance"])
    ChargeProfile.objects.create(
        equipment=s["eq"], user_type=UserType.FACULTY, profile_type="SAMPLE", time_formula="A*45",
        primary_unit_charge=Decimal("100.00"),
    )
    urg = _submit(faculty_requester, s["eq"])
    assert not urg.pending_supervisor_approval
    alerts = _oic_alerts(emails)
    assert _recipient_ids(alerts) == sorted([s["oic"].id, s["temp_oic"].id])
    assert alerts[0]["template_context"]["status_label"] == "Ready to allocate"


def test_default_template_renders_the_request_details():
    from iic_booking.equipment.booking_lab_messages import ensure_email_template

    ensure_email_template(EMAIL_TEMPLATE_CODE)
    tpl = CommunicationTemplate.objects.get(code=EMAIL_TEMPLATE_CODE, communication_type="email")
    ctx = Context({
        "user_name": "OIC", "headline": "New urgent booking request", "intro": "Intro text", "request_id": 42,
        "status_label": "Ready to allocate", "equipment_name": "XRD", "equipment_code": "XRD1",
        "requester_name": "Requester", "requester_category": "Student", "required_time": "1 h 30 min",
        "amount": "₹150.00", "preferred_schedule": "Mornings", "reason": "Reviewer deadline",
        "link": "https://portal.example/urgent-requests?request=42",
    })
    subject = Template(tpl.subject).render(ctx)
    html = Template(tpl.body_html).render(ctx)
    assert "#42" in subject and "XRD" in subject
    for text in ("1 h 30 min", "₹150.00", "Mornings", "Reviewer deadline", "Student", "urgent-requests?request=42"):
        assert text in html

    tpl.subject = "Customised"
    tpl.save(update_fields=["subject"])
    ensure_email_template(EMAIL_TEMPLATE_CODE)
    assert CommunicationTemplate.objects.get(pk=tpl.pk).subject == "Customised"


# ---------------------------------------------------------------- sign-in attention items


def _items(user):
    return {i["key"]: i for i in collect_pending_actions(user)}


def _ready_request(s, **fields):
    return UrgentBookingRequest.objects.create(
        user=s["student"], equipment=s["eq"], request_type="REVIEWER_URGENT", disclaimer_accepted=True,
        reviewer_comment=REASON, supervisor=s["faculty"], supervisor_approval_required=True,
        supervisor_decision="APPROVED", requires_slot_allocation=True, duration_minutes=90, **fields,
    )


def test_urgent_item_lists_requests_with_deep_links_for_oic_and_temp_oic_only():
    s = _setup()
    urg = _ready_request(s)
    for user in (s["oic"], s["temp_oic"]):
        item = _items(user)["urgent_requests"]
        assert item["count"] == 1
        entry = item["entries"][0]
        assert entry["link"] == f"/urgent-requests?request={urg.id}"
        assert f"#{urg.id}" in entry["label"] and "Alert EQ" in entry["label"]
        assert str(s["student"].get_user_type_display_label()) in entry["label"]
        assert item["details"] == [entry["label"]]
    for user in (s["other_oic"], s["ended_temp"]):
        assert "urgent_requests" not in _items(user)


def _booking(s):
    return Booking.objects.create(
        user=s["student"], equipment=s["eq"], charge_profile=ChargeProfile.objects.filter(equipment=s["eq"]).first(),
        status=BookingStatus.BOOKED, total_charge=Decimal("10.00"), total_time_minutes=60,
        virtual_booking_id=f"IIC{s['eq'].code}{uuid.uuid4().hex[:4]}", user_type_snapshot=UserType.STUDENT,
    )


def _lab_message_notice(recipient, booking, text, kind="user"):
    return CommunicationService.send_push_notification(
        recipient=recipient,
        title="Message from booking user",
        message=f"Alert EQ: {text}",
        metadata={
            "event": "booking.lab_message", "lab_message": kind, "real_booking_id": booking.booking_id,
            "booking_id": booking.virtual_booking_id, "staff_recipient": True,
        },
    )


def _push_rows(user):
    return CommunicationLog.objects.filter(
        recipient=user, communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION
    ).order_by("id")


def test_lab_message_item_groups_unread_messages_per_booking_with_message_links():
    s = _setup()
    first, second, answered, read = (_booking(s) for _ in range(4))
    _lab_message_notice(s["oic"], first, "Older note")
    _lab_message_notice(s["oic"], first, "Can I add one more sample?")
    _lab_message_notice(s["oic"], second, "Is the instrument free?")
    _lab_message_notice(s["oic"], answered, "Please confirm")
    BookingEvent.objects.create(
        booking=answered, event_type="COMMENT", comment="Confirmed", created_by=s["oic"],
        metadata={"lab_message": "staff_reply"},
    )
    _lab_message_notice(s["oic"], read, "Seen already")
    _push_rows(s["oic"]).filter(metadata__real_booking_id=read.booking_id).update(
        status=CommunicationLog.CommunicationStatus.READ
    )
    _lab_message_notice(s["other_oic"], _booking(s), "Not for this OIC")

    item = _items(s["oic"])["lab_messages_from_users"]
    assert item["count"] == 2
    by_link = {e["link"]: e for e in item["entries"]}
    first_link = f"/booking-management?expand={first.booking_id}&section=messages"
    assert set(by_link) == {first_link, f"/booking-management?expand={second.booking_id}&section=messages"}
    assert "Can I add one more sample?" in by_link[first_link]["label"]
    assert first.virtual_booking_id in by_link[first_link]["label"]
    assert len(by_link[first_link]["notification_ids"]) == 2

    assert "lab_messages_from_users" not in _items(s["temp_oic"])


def test_staff_items_use_a_fixed_number_of_queries():
    s = _setup()

    def run():
        with CaptureQueriesContext(connection) as ctx:
            collect_pending_actions(s["oic"])
        return len(ctx.captured_queries)

    _ready_request(s)
    _lab_message_notice(s["oic"], _booking(s), "One")
    baseline = run()
    for i in range(4):
        _ready_request(s)
        _lab_message_notice(s["oic"], _booking(s), f"More {i}")
    assert run() == baseline
