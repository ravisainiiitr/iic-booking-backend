"""Booking.created_via: where a booking was made (app, browser, system)."""

from types import SimpleNamespace

import pytest
from django.test import RequestFactory

from iic_booking.equipment.booking_audit import SOURCE_BACKGROUND_JOB, SOURCE_WEB, audit_scope
from iic_booking.equipment.booking_channel import BookingChannel as C
from iic_booking.equipment.booking_channel import channel_for_request, channel_from_user_agent
from iic_booking.facility_groups.tests.conftest import make_booking, make_equipment, make_user
from iic_booking.users.models.mobile_device_session import MobileDeviceSession

ANDROID_APP = ("Mozilla/5.0 (Linux; Android 14; Pixel 7; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
               "Chrome/129.0 Mobile Safari/537.36 IICBookingApp/1.0")
IOS_APP = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 IICBookingApp/1.0"
DESKTOP = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
PHONE = "Mozilla/5.0 (Linux; Android 13; SM-A536E) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"
IPHONE_SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Version/17.5 Mobile/15E148 Safari/604.1"
IPAD = "Mozilla/5.0 (iPad; CPU OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Version/17.0 Mobile/15E148 Safari/604.1"
ANDROID_TABLET = "Mozilla/5.0 (Linux; Android 13; SM-X200) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


@pytest.mark.parametrize("ua,expected", [
    (ANDROID_APP, C.ANDROID_APP), (IOS_APP, C.IOS_APP), (DESKTOP, C.DESKTOP_WEB), (PHONE, C.MOBILE_WEB),
    (IPHONE_SAFARI, C.MOBILE_WEB), (IPAD, C.TABLET_WEB), (ANDROID_TABLET, C.TABLET_WEB),
    ("curl/8.4.0", C.API_CLIENT), ("", C.API_CLIENT),
])
def test_user_agent_classification(ua, expected):
    assert channel_from_user_agent(ua) == expected


def test_app_session_decides_the_platform_even_without_the_marker():
    req = RequestFactory().post("/api/bookings/", HTTP_USER_AGENT=DESKTOP)
    req.auth = MobileDeviceSession(platform=MobileDeviceSession.PLATFORM_IOS)
    assert channel_for_request(req) == C.IOS_APP
    req.auth = MobileDeviceSession(platform=MobileDeviceSession.PLATFORM_ANDROID)
    assert channel_for_request(req) == C.ANDROID_APP
    req.auth = SimpleNamespace(key="web-token")
    assert channel_for_request(req) == C.DESKTOP_WEB


@pytest.mark.django_db
def test_new_bookings_are_stamped_once_with_where_they_were_made():
    user = make_user(user_type="student")
    eq = make_equipment()

    req = RequestFactory().post("/api/equipments/1/book/", HTTP_USER_AGENT=ANDROID_APP)
    with audit_scope(source=SOURCE_WEB, request=req):
        b = make_booking(user, eq)
    assert b.created_via == C.ANDROID_APP

    # Later changes (e.g. staff editing from a desktop) keep the original channel.
    other = RequestFactory().post("/api/bookings/1/reschedule/", HTTP_USER_AGENT=DESKTOP)
    with audit_scope(source=SOURCE_WEB, request=other):
        b.total_time_minutes = 90
        b.save()
    b.refresh_from_db()
    assert b.created_via == C.ANDROID_APP

    with audit_scope(source=SOURCE_BACKGROUND_JOB, source_label="job"):
        assert make_booking(user, eq).created_via == C.SYSTEM
    assert make_booking(user, eq).created_via == C.SYSTEM  # script / shell: no request
    assert make_booking(user, eq, created_via=C.TABLET_WEB).created_via == C.TABLET_WEB
