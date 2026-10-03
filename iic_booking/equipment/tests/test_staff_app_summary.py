"""IIC Booking app "Today" summary for Officers In Charge and Lab Operators: scoping, counts and caching."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core.cache import cache
from django.utils import timezone

from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    SampleTraceStatus,
    UrgentBookingRequest,
    WaitlistEntry,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

URL = "/api/staff-app/today/"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _today_at(hour, days=0):
    base = timezone.localtime(timezone.now()) + timedelta(days=days)
    return base.replace(hour=hour, minute=0, second=0, microsecond=0)


@pytest.fixture
def lab(egs_factory):
    mine = egs_factory.equipment(name="Mine XRD")
    other = egs_factory.equipment(name="Other SEM")
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    EquipmentManager.objects.create(equipment=mine, manager=oic)
    EquipmentOperator.objects.create(equipment=mine, operator=operator)
    student = egs_factory.student()
    return SimpleNamespace(f=egs_factory, mine=mine, other=other, oic=oic, operator=operator, student=student)


def _get(lab, user, **params):
    return lab.f.client_for(user).get(URL, params)


def test_only_oic_and_lab_operator(lab):
    for user_type in (UserType.FACULTY, UserType.STUDENT, UserType.ADMIN, UserType.DEPT_ADMIN, UserType.FINANCE):
        resp = _get(lab, UserFactory(user_type=user_type, admin_approved=True))
        assert resp.status_code == 403, user_type
    assert lab.f.client_for(None).get(URL).status_code in (401, 403)


def test_today_and_tomorrow_scoped_to_my_equipment(lab):
    today_booking = lab.f.booking(lab.student, lab.mine, _today_at(23))
    tomorrow_booking = lab.f.booking(lab.student, lab.mine, _today_at(10, days=1))
    lab.f.booking(lab.student, lab.mine, _today_at(10, days=3))
    lab.f.booking(lab.student, lab.other, _today_at(23))
    cancelled = lab.f.booking(lab.student, lab.mine, _today_at(22))
    cancelled.status = BookingStatus.CANCELLED
    cancelled.save(update_fields=["status"])

    for user in (lab.operator, lab.oic):
        resp = _get(lab, user, refresh=1)
        assert resp.status_code == 200, resp.content
        data = resp.data
        assert [d["label"] for d in data["days"]] == ["Today", "Tomorrow"]
        assert [b["booking_id"] for b in data["days"][0]["bookings"]] == [today_booking.booking_id]
        assert [b["booking_id"] for b in data["days"][1]["bookings"]] == [tomorrow_booking.booking_id]
        assert [e["equipment_id"] for e in data["equipment"]] == [lab.mine.equipment_id]
        row = data["days"][0]["bookings"][0]
        assert row["booking_ref"] == today_booking.virtual_booking_id
        assert row["start_time"] and row["end_time"]
        assert "sample_summary" in row


def test_sample_stage_and_awaiting_receipt_count(lab):
    booking = lab.f.booking(lab.student, lab.mine, _today_at(23))
    BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_SENT)
    received = lab.f.booking(lab.student, lab.mine, _today_at(10, days=1))
    BookingSampleTrace.objects.create(booking=received, status=SampleTraceStatus.SAMPLE_SENT)
    BookingSampleTrace.objects.create(booking=received, status=SampleTraceStatus.SAMPLE_ACCEPTED)
    elsewhere = lab.f.booking(lab.student, lab.other, _today_at(23))
    BookingSampleTrace.objects.create(booking=elsewhere, status=SampleTraceStatus.SAMPLE_SENT)

    data = _get(lab, lab.operator).data

    assert data["counts"]["samples_awaiting_receipt"] == 1
    assert data["days"][0]["bookings"][0]["sample_stage"] == SampleTraceStatus.SAMPLE_SENT
    assert data["days"][1]["bookings"][0]["sample_stage"] == SampleTraceStatus.SAMPLE_ACCEPTED


def test_user_messages_awaiting_reply(lab):
    unanswered = lab.f.booking(lab.student, lab.mine, _today_at(10, days=2))
    answered = lab.f.booking(lab.student, lab.mine, _today_at(11, days=2))
    elsewhere = lab.f.booking(lab.student, lab.other, _today_at(10, days=2))
    for booking, kinds in ((unanswered, ["user"]), (answered, ["user", "staff_reply"]), (elsewhere, ["user"])):
        for kind in kinds:
            BookingEvent.objects.create(
                booking=booking,
                event_type=BookingEventType.COMMENT,
                comment="hello",
                metadata={"lab_message": kind},
            )

    data = _get(lab, lab.oic).data

    assert data["counts"]["user_messages_awaiting_reply"] == 1
    assert data["message_booking_ids"] == [unanswered.booking_id]


def test_oic_counts_urgent_and_waitlist_for_own_equipment_only(lab):
    for equipment in (lab.mine, lab.other):
        UrgentBookingRequest.objects.create(user=lab.student, equipment=equipment)
        WaitlistEntry.objects.create(user=lab.student, equipment=equipment)
    WaitlistEntry.objects.create(user=lab.f.student(), equipment=lab.mine, status="OPT_OUT")

    oic = _get(lab, lab.oic).data["counts"]
    assert oic["urgent_requests_pending"] == 1
    assert oic["waitlist_active"] == 1

    operator = _get(lab, lab.operator).data["counts"]
    assert operator["urgent_requests_pending"] is None
    assert operator["waitlist_active"] is None


def test_temporary_oic_sees_delegated_equipment(lab):
    temp = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentTemporaryOIC.objects.create(
        equipment=lab.mine, primary_oic=lab.oic, temporary_oic=temp, resume_at=timezone.now() + timedelta(days=2)
    )
    booking = lab.f.booking(lab.student, lab.mine, _today_at(23))

    data = _get(lab, temp).data

    assert [b["booking_id"] for b in data["days"][0]["bookings"]] == [booking.booking_id]


def test_unassigned_staff_get_empty_summary(lab):
    lab.f.booking(lab.student, lab.mine, _today_at(23))
    lone = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)

    data = _get(lab, lone).data

    assert data["equipment"] == []
    assert all(not d["bookings"] for d in data["days"])


def test_summary_is_cached_briefly_and_refresh_bypasses(lab):
    assert _get(lab, lab.operator).data["days"][0]["bookings"] == []
    booking = lab.f.booking(lab.student, lab.mine, _today_at(23))

    assert _get(lab, lab.operator).data["days"][0]["bookings"] == []
    fresh = _get(lab, lab.operator, refresh=1).data
    assert [b["booking_id"] for b in fresh["days"][0]["bookings"]] == [booking.booking_id]
