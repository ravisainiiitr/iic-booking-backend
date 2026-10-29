"""I-STEM's hourly slot grid, the occupied-slot retry, the silent-mode flag and the full-profile user lookup."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest
from django.utils import timezone

from iic_booking.istem import contract, otp_link
from iic_booking.istem.choices import ErrorCategory, JobState, SyncState, UserLinkState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.mapping import istem_hour_window, istem_window_fits_day
from iic_booking.istem.models import IstemUserLink
from iic_booking.istem.otp_link import OtpLinkError
from iic_booking.users.models.billing import ExternalBillingProfile
from iic_booking.users.tests.factories import UserFactory

from .support import FakeTransport, json_response, ready_booking, token_response
from .test_confirm import CONFIRMED, _confirm, _created, _fresh_tokens, live  # noqa: F401

pytestmark = pytest.mark.django_db


def _ist(day, hh, mm=0):
    return datetime(day.year, day.month, day.day, hh, mm, tzinfo=contract.IST)


@pytest.mark.parametrize("start,end,shift,expected", [
    ((10, 30), (12, 0), 0, ("10:00", "12:00")),
    ((9, 0), (9, 30), 0, ("09:00", "10:00")),
    ((14, 0), (15, 30), 0, ("14:00", "16:00")),
    ((10, 30), (12, 0), 2, ("12:00", "14:00")),
])
def test_bookings_of_any_length_map_to_covering_hourly_slots(start, end, shift, expected):
    day = timezone.now().date()
    first, last = istem_hour_window(_ist(day, *start), _ist(day, *end), shift)
    assert (first.strftime("%H:%M"), last.strftime("%H:%M")) == expected


def test_a_shift_never_crosses_midnight():
    day = timezone.now().date()
    assert istem_window_fits_day(_ist(day, 21, 30), _ist(day, 22, 30), 1)  # 22:00-24:00
    assert not istem_window_fits_day(_ist(day, 21, 30), _ist(day, 22, 30), 2)


def _booking_at(hh, mm, minutes):
    booking = ready_booking()
    slot = booking.daily_slots.get()
    start = _ist(slot.start_datetime.date(), hh, mm)
    type(slot).objects.filter(pk=slot.pk).update(start_datetime=start, end_datetime=start + timedelta(minutes=minutes))
    return booking


def test_create_and_confirm_send_the_hourly_window(live):
    link = _created(_booking_at(10, 30, 90))
    assert (link.create_payload["preferred_timings_from"][11:16], link.create_payload["preferred_timings_to"][11:16]) == ("10:00", "12:00")
    transport = FakeTransport(json_response(CONFIRMED))
    assert _confirm(link, transport) == JobState.DONE
    sent = transport.calls[-1]["data"]
    assert (sent["allocated_timings_from"][11:16], sent["allocated_timings_to"][11:16]) == ("10:00", "12:00")


def test_occupied_hourly_slot_moves_to_the_next_hour_of_the_day(live):
    link = _created(_booking_at(10, 30, 90))
    refused = json_response({"status": "ERROR", "message": "Slot already booked for this equipment"})
    assert _confirm(link, FakeTransport(refused)) == JobState.RETRY_WAIT
    assert (link.sync_state, link.istem_slot_shift_hours) == (SyncState.CONFIRM_READY, 1)


def test_no_free_hour_left_that_day_holds_for_staff(live):
    link = _created(_booking_at(22, 30, 60))  # 22:00-24:00; one hour later would cross midnight
    refused = json_response({"status": "ERROR", "message": "Slot already booked for this equipment"})
    assert _confirm(link, FakeTransport(refused)) == JobState.BLOCKED
    assert link.sync_state == SyncState.CONFIRM_FAILED and "hourly slot" in link.review_reason


def test_silent_mode_flag_rides_on_booking_calls_only(live):
    live.ISTEM_SILENT_MODE_FIELD = "silent"
    transport = FakeTransport(token_response(), json_response({"status": "Sucess", "message": "Done", "new_service_id": 1, "fbr_no": "19761"}),
                              json_response({"status": "SUCCESS", "message": "ok", "user_id": 5}))
    client = IstemClient(transport)
    client.create_booking({"booking_remark": "x"})
    client.create_user({"user_email": "a@example.com"})
    assert transport.calls[1]["data"]["silent"] == "1"
    assert "silent" not in transport.calls[2]["data"]


# --- full-profile user lookup ----------------------------------------------------------------------------------------

LOOKUP = {"status": "SUCCESS", "message": "User found", "result": [{
    "user_id": "60016", "user_name": "Asha Verma", "user_email": "Asha@Lab.example.com", "user_contactno": "9876543210",
    "user_organisation": "Asha Labs", "user_department": "Materials", "user_address": "12 Station Road",
    "user_city": "Roorkee", "user_state": "Uttarakhand", "user_pin": "247667", "user_country": "India",
    "gstin": "05ABCDE1234F1Z5", "billing_user_name": "Asha Labs", "user_type": "Industry User"}]}


def test_lookup_returns_the_profile_istem_sends(live):
    rows = IstemClient(FakeTransport(token_response(), json_response(LOOKUP))).lookup_public_user(user_email="asha@lab.example.com")
    profile = rows[0]["profile"]
    assert profile["email"] == "asha@lab.example.com"
    assert (profile["organisation"], profile["department"], profile["state"], profile["gstin"], profile["user_type"]) == (
        "Asha Labs", "Materials", "Uttarakhand", "05ABCDE1234F1Z5", "Industry User")


def test_lookup_reads_the_staging_row_shape(live):
    # [STAGING] result is a single object, names are user_firstname / user_lastname padded with spaces.
    staging = {"status": "SUCCESS", "message": "User found", "result": {
        "user_id": "22001", "user_name": "PU_ASHAVERM001", "user_salutation": "Ms.", "user_firstname": "Asha" + " " * 40,
        "user_lastname": "Verma" + " " * 40, "user_email": "asha@lab.example.com", "user_contactno": "9876543210",
        "user_category": "External", "user_type": "Academic User", "state": "Uttarakhand", "user_district": "Haridwar",
        "user_department": "Materials", "user_designation": "Research Scholar", "gstin": "", "user_status": "1"}}
    rows = IstemClient(FakeTransport(token_response(), json_response(staging))).lookup_public_user(user_contactno="9876543210")
    assert [r["user_id"] for r in rows] == ["22001"]
    profile = rows[0]["profile"]
    assert (profile["first_name"], profile["last_name"], profile["salutation"], profile["designation"]) == (
        "Asha", "Verma", "Ms.", "Research Scholar")
    assert (profile["state"], profile["user_type"], profile["district"]) == ("Uttarakhand", "Academic User", "Haridwar")
    assert "gstin" not in profile


def test_lookup_with_no_match_is_an_empty_list(live):
    body = {"status": "ERROR", "message": "No matching user found"}
    assert IstemClient(FakeTransport(token_response(), json_response(body))).lookup_public_user(user_email="x@example.com") == []


@pytest.fixture
def otp_on(live):
    live.ISTEM_OTP_LINKING_ENABLED = True
    return live


def test_existing_user_registers_with_email_and_otp_and_gets_the_profile(otp_on, mailoutbox):
    user = UserFactory()
    found = otp_link.find_existing_account(user, email="asha@lab.example.com",
                                           client_factory=lambda: IstemClient(FakeTransport(token_response(), json_response(LOOKUP))))
    assert found["email_masked"] == "a***@lab.example.com"
    assert otp_link.link_status(user).get("istem_profile") is None  # nothing shown before the OTP
    otp_link.send_otp(user)
    assert mailoutbox[-1].to == ["asha@lab.example.com"]
    otp_link.verify_otp(user, re.search(r"\b(\d{6})\b", mailoutbox[-1].body).group(1))
    link = IstemUserLink.objects.get(user=user)
    assert (link.state, link.public_user_id) == (UserLinkState.LINKED, "60016")
    assert otp_link.link_status(user)["istem_profile"]["organisation"] == "Asha Labs"
    billing = ExternalBillingProfile.objects.get(user=user)
    assert (billing.gstin, billing.billing_city, billing.billing_state) == ("05ABCDE1234F1Z5", "Roorkee", "Uttarakhand")


def test_lookup_without_an_email_cannot_send_an_otp(otp_on):
    row = {k: v for k, v in LOOKUP["result"][0].items() if k != "user_email"}
    body = {**LOOKUP, "result": [row]}
    with pytest.raises(OtpLinkError) as exc:
        otp_link.find_existing_account(UserFactory(), mobile="98765 43210",
                                       client_factory=lambda: IstemClient(FakeTransport(token_response(), json_response(body))))
    assert exc.value.code == "istem_email_missing"


def test_lookup_not_deployed_is_reported_as_unavailable(otp_on):
    from iic_booking.istem.client import TransportResponse

    transport = FakeTransport(token_response(), TransportResponse(404, "<html>Not Found</html>"))
    with pytest.raises(OtpLinkError) as exc:
        otp_link.find_existing_account(UserFactory(), email="a@example.com", client_factory=lambda: IstemClient(transport))
    assert exc.value.code in ("lookup_unavailable", "lookup_failed")


def test_nothing_found_and_ambiguous_matches_are_refused(otp_on):
    nothing = {"status": "ERROR", "message": "User not found"}
    with pytest.raises(OtpLinkError) as exc:
        otp_link.find_existing_account(UserFactory(), email="x@example.com",
                                       client_factory=lambda: IstemClient(FakeTransport(token_response(), json_response(nothing))))
    assert exc.value.code == "not_found"
    clear_token_cache()
    two = {**LOOKUP, "result": [LOOKUP["result"][0], {**LOOKUP["result"][0], "user_id": "60017"}]}
    with pytest.raises(OtpLinkError) as exc:
        otp_link.find_existing_account(UserFactory(), mobile="9876543210",
                                       client_factory=lambda: IstemClient(FakeTransport(token_response(), json_response(two))))
    assert exc.value.code == "ambiguous"
