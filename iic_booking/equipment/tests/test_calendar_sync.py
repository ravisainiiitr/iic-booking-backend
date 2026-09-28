"""Calendar sync: iCalendar subscription feed + per-booking .ics for internal/external booking users only."""

from __future__ import annotations

import itertools
import uuid
from datetime import timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.calendar_sync import is_calendar_sync_eligible
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    CalendarFeedToken,
    ChargeProfile,
    DailySlot,
    Equipment,
    SlotMaster,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

_SLOT_NUMBERS = itertools.count(1)
STAFF_TYPES = [UserType.ADMIN, UserType.DEPT_ADMIN, UserType.MANAGER, UserType.OPERATOR, UserType.FINANCE]


def _client(user=None) -> APIClient:
    c = APIClient()
    if user is not None:
        c.force_authenticate(user=user)
    return c


@pytest.fixture
def equipment():
    eq = Equipment.objects.create(
        name="X-Ray Diffractometer",
        code=f"CS{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        status="ACTIVE",
        location="Block C, Room 12, IIC",
    )
    ChargeProfile.objects.create(equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00"))
    return eq


@pytest.fixture
def student():
    return UserFactory(user_type=UserType.STUDENT, admin_approved=True)


def _booking(user, eq, status, hour_offsets):
    booking = Booking.objects.create(
        user=user,
        equipment=eq,
        charge_profile=ChargeProfile.objects.filter(equipment=eq).first(),
        status=status,
        total_charge=Decimal("10.00"),
        total_time_minutes=60 * len(hour_offsets),
        virtual_booking_id=f"IIC{eq.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )
    base = (timezone.now() + timedelta(days=3)).replace(minute=0, second=0, microsecond=0)
    for offset in hour_offsets:
        start = base + timedelta(hours=offset)
        master = SlotMaster.objects.create(
            equipment=eq,
            slot_number=next(_SLOT_NUMBERS),
            open_time=timezone.localtime(start).time(),
            close_time=timezone.localtime(start + timedelta(hours=1)).time(),
            is_active=True,
        )
        DailySlot.objects.create(
            slot_master=master,
            date=timezone.localtime(start).date(),
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            status="BOOKED",
            booking=booking,
        )
    return booking


def _unfold(body: str) -> str:
    return body.replace("\r\n ", "")


@pytest.mark.parametrize(
    "user_type",
    [UserType.STUDENT, UserType.FACULTY, UserType.EXTERNAL, UserType.RND, UserType.INSTITUTE, "industry"],
)
def test_internal_and_external_users_are_eligible(user_type):
    assert is_calendar_sync_eligible(UserFactory(user_type=user_type, admin_approved=True)) is True


@pytest.mark.parametrize("user_type", STAFF_TYPES + [UserType.ORG_ADMIN, UserType.EXTERNAL_RELATIONS])
def test_staff_roles_are_not_eligible(user_type):
    assert is_calendar_sync_eligible(UserFactory(user_type=user_type, admin_approved=True)) is False


def test_unapproved_user_is_not_eligible():
    user = UserFactory(user_type=UserType.STUDENT, admin_approved=False)
    assert user.is_active is False
    assert is_calendar_sync_eligible(user) is False


def test_settings_creates_stable_token_and_subscription_links(student):
    res = _client(student).get("/api/calendar-sync/")
    assert res.status_code == 200, res.data
    token = CalendarFeedToken.objects.get(user=student).token
    assert res.data["eligible"] is True
    assert res.data["feed_url"].endswith(f"/api/calendar/feed/{token}.ics")
    assert res.data["webcal_url"].startswith("webcal://")
    assert res.data["google_url"].startswith("https://calendar.google.com/calendar/r?cid=webcal%3A%2F%2F")
    assert res.data["outlook_url"].startswith("https://outlook.live.com/calendar/0/addfromweb?url=")

    again = _client(student).get("/api/calendar-sync/")
    assert again.data["feed_url"] == res.data["feed_url"]


@pytest.mark.parametrize("user_type", STAFF_TYPES)
def test_staff_are_refused(user_type, equipment):
    staff = UserFactory(user_type=user_type, admin_approved=True)
    booking = _booking(staff, equipment, BookingStatus.BOOKED, [0])
    client = _client(staff)

    assert client.get("/api/calendar-sync/").status_code == 403
    assert client.post("/api/calendar-sync/regenerate/").status_code == 403
    assert client.get(f"/api/bookings/{booking.booking_id}/calendar.ics").status_code == 403
    assert not CalendarFeedToken.objects.filter(user=staff).exists()


def test_regenerate_revokes_old_feed_url(student):
    first = _client(student).get("/api/calendar-sync/").data["feed_url"]
    old_token = CalendarFeedToken.objects.get(user=student).token

    res = _client(student).post("/api/calendar-sync/regenerate/")
    assert res.status_code == 200
    assert res.data["feed_url"] != first
    assert _client().get(f"/api/calendar/feed/{old_token}.ics").status_code == 404
    new_token = CalendarFeedToken.objects.get(user=student).token
    assert _client().get(f"/api/calendar/feed/{new_token}.ics").status_code == 200


def test_feed_lists_active_bookings_and_drops_cancelled_ones(student, equipment):
    booked = _booking(student, equipment, BookingStatus.BOOKED, [0, 1, 5])
    pending = _booking(student, equipment, BookingStatus.PENDING_PAYMENT, [24])
    cancelled = _booking(student, equipment, BookingStatus.CANCELLED, [30])
    someone_else = _booking(UserFactory(user_type=UserType.STUDENT), equipment, BookingStatus.BOOKED, [40])
    token = CalendarFeedToken.objects.create(user=student, token="feed-token-abc")

    res = _client().get(f"/api/calendar/feed/{token.token}.ics")

    assert res.status_code == 200
    assert res["Content-Type"].startswith("text/calendar")
    raw = res.content.decode("utf-8")
    assert raw.startswith("BEGIN:VCALENDAR\r\n") and raw.endswith("END:VCALENDAR\r\n")
    assert all(len(line.encode("utf-8")) <= 75 for line in raw.split("\r\n"))
    body = _unfold(raw)
    # Contiguous slots 0-1 merge; slot 5 is a separate event.
    assert f"UID:booking-{booked.booking_id}-1@" in body
    assert f"UID:booking-{booked.booking_id}-2@" in body
    assert f"UID:booking-{booked.booking_id}-3@" not in body
    assert f"UID:booking-{pending.booking_id}-1@" in body
    assert "awaiting payment" in body
    assert "STATUS:TENTATIVE" in body and "STATUS:CONFIRMED" in body
    assert f"booking-{cancelled.booking_id}-" not in body
    assert f"booking-{someone_else.booking_id}-" not in body
    assert "LOCATION:Block C\\, Room 12\\, IIC" in body
    assert "X-Ray Diffractometer" in body
    token.refresh_from_db()
    assert token.last_accessed_at is not None


def test_merged_event_spans_both_contiguous_slots(student, equipment):
    booking = _booking(student, equipment, BookingStatus.BOOKED, [0, 1])
    CalendarFeedToken.objects.create(user=student, token="span-token")
    body = _unfold(_client().get("/api/calendar/feed/span-token.ics").content.decode())
    slots = sorted(booking.daily_slots.all(), key=lambda s: s.start_datetime)
    fmt = "%Y%m%dT%H%M%SZ"
    assert f"DTSTART:{slots[0].start_datetime.astimezone(dt_timezone.utc).strftime(fmt)}" in body
    assert f"DTEND:{slots[1].end_datetime.astimezone(dt_timezone.utc).strftime(fmt)}" in body


def test_unknown_token_is_404():
    assert _client().get("/api/calendar/feed/does-not-exist.ics").status_code == 404


def test_feed_stops_when_user_becomes_staff(student):
    CalendarFeedToken.objects.create(user=student, token="promoted-token")
    student.user_type = UserType.MANAGER
    student.save(update_fields=["user_type"])
    assert _client().get("/api/calendar/feed/promoted-token.ics").status_code == 404


def test_single_booking_download_is_owner_only(student, equipment):
    booking = _booking(student, equipment, BookingStatus.BOOKED, [0])

    res = _client(student).get(f"/api/bookings/{booking.booking_id}/calendar.ics")
    assert res.status_code == 200
    assert "attachment;" in res["Content-Disposition"]
    body = _unfold(res.content.decode())
    assert f"UID:booking-{booking.booking_id}-1@" in body
    assert "BEGIN:VALARM" in body

    other = UserFactory(user_type=UserType.STUDENT, admin_approved=True)
    assert _client(other).get(f"/api/bookings/{booking.booking_id}/calendar.ics").status_code == 404


def test_single_booking_download_rejects_cancelled(student, equipment):
    booking = _booking(student, equipment, BookingStatus.CANCELLED, [0])
    assert _client(student).get(f"/api/bookings/{booking.booking_id}/calendar.ics").status_code == 400
