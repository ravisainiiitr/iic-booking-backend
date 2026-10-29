"""Linking a user already registered on I-STEM by an OTP sent to the I-STEM email (no network, no staff step)."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.istem import otp_link, service
from iic_booking.istem.choices import EvidenceType, JobState, LinkOtpState, Operation, SyncState, UserLinkState
from iic_booking.istem.client import IstemClient
from iic_booking.istem.models import IstemBookingLink, IstemLinkOtp, IstemOutboxJob, IstemSyncEvent, IstemUserLink
from iic_booking.istem.otp_link import OtpLinkError
from iic_booking.users.tests.factories import UserFactory

from .support import FakeTransport, json_response, token_response
from .test_service import _booking_with_unlinked_user, _enable_user_creation, _fresh_tokens, complete_profile, live  # noqa: F401

pytestmark = pytest.mark.django_db

ISTEM_EMAIL = "asha.registered@example.com"


@pytest.fixture
def otp_on(live):
    live.ISTEM_OTP_LINKING_ENABLED = True
    return live


def _awaiting(user=None, public_user_id="60016", email=ISTEM_EMAIL):
    user = user or UserFactory()
    return IstemUserLink.objects.create(user=user, state=UserLinkState.AWAITING_OTP,
                                        candidate_public_user_id=public_user_id, candidate_email=email)


def _code(mailoutbox) -> str:
    return re.search(r"\b(\d{6})\b", mailoutbox[-1].body).group(1)


def _wrong(code: str) -> str:
    return f"{(int(code) + 1) % 10 ** 6:06d}"


def _run_duplicate(settings, body):
    booking = _booking_with_unlinked_user()
    _enable_user_creation(settings, booking.user)
    service.enqueue_user_link(booking.user, trigger="test")
    job = IstemOutboxJob.objects.get(operation=Operation.ENSURE_USER)
    transport = FakeTransport(token_response(), json_response(body))
    assert service.process_job(job.pk, client_factory=lambda: IstemClient(transport)) == JobState.DONE
    return booking, IstemUserLink.objects.get(user=booking.user)


# --- Create User answers "already registered" ------------------------------------------------------------------

def test_duplicate_by_email_waits_for_otp_to_the_email_sent(otp_on, complete_profile):
    booking, link = _run_duplicate(otp_on, {"result": "ERROR", "message": "EmailId already exists",
                                            "data": [{"user_id": "60016", "user_name": "PU_X 001"}]})
    assert link.state == UserLinkState.AWAITING_OTP
    assert (link.candidate_public_user_id, link.candidate_email) == ("60016", booking.user.email.lower())
    assert link.public_user_id is None
    # Waiting for the OTP is not a reason to call Create User again.
    service.enqueue_user_link(booking.user, trigger="again")
    assert not IstemOutboxJob.objects.filter(operation=Operation.ENSURE_USER, state=JobState.PENDING).exists()


def test_duplicate_uses_the_email_registered_on_istem_when_returned(otp_on, complete_profile):
    _, link = _run_duplicate(otp_on, {"result": "ERROR", "message": "Mobile number already exists",
                                      "data": [{"user_id": "60016", "user_email": "Old.Address@Example.com"}]})
    assert (link.state, link.candidate_email) == (UserLinkState.AWAITING_OTP, "old.address@example.com")


def test_full_duplicate_row_is_kept_and_applied_only_after_the_otp(otp_on, complete_profile, mailoutbox):
    # [STAGING] since 2026-10-29 data[0] is the user's full record, the same as the lookup row.
    row = {"user_id": "60016", "user_name": "PU_ASHAVERM001", "user_firstname": "Asha" + " " * 40,
           "user_lastname": "Verma" + " " * 40, "user_email": ISTEM_EMAIL, "user_contactno": "9876543210",
           "user_organisation": "Asha Labs", "user_department": "Materials", "user_address": "12 Station Road",
           "user_city": "Roorkee", "user_district": "Haridwar", "state": "Uttarakhand", "user_pin": "247667",
           "user_country": "India", "user_type": "Academic User", "gstin": "", "user_status": "1"}
    booking, link = _run_duplicate(otp_on, {"result": "ERROR", "message": "Mobile number already exists", "data": [row]})
    assert (link.state, link.candidate_email) == (UserLinkState.AWAITING_OTP, ISTEM_EMAIL)
    assert (link.candidate_profile["first_name"], link.candidate_profile["state"]) == ("Asha", "Uttarakhand")
    assert not link.istem_profile  # nothing is used before the OTP proves the email
    otp_link.send_otp(booking.user)
    otp_link.verify_otp(booking.user, _code(mailoutbox))
    link.refresh_from_db()
    assert (link.state, link.public_user_id) == (UserLinkState.LINKED, "60016")
    assert (link.istem_profile["organisation"], link.candidate_profile) == ("Asha Labs", {})


def test_staging_duplicate_by_mobile_without_email_needs_review(otp_on, complete_profile):
    # [STAGING] the row carries no email, and the mobile matched: there is no address to prove.
    _, link = _run_duplicate(otp_on, {"result": "ERROR", "message": "Mobile number already exists",
                                      "data": [{"user_id": "60016", "user_name": "PU_X 001", "user_status": "1"}]})
    assert link.state == UserLinkState.NEEDS_REVIEW
    assert "60016" in link.review_reason


def test_duplicate_without_otp_linking_keeps_the_review_path(live, complete_profile):
    _, link = _run_duplicate(live, {"result": "ERROR", "message": "EmailId already exists", "data": [{"user_id": "60016"}]})
    assert link.state == UserLinkState.NEEDS_REVIEW


def test_duplicate_of_an_istem_user_linked_elsewhere_is_refused(otp_on, complete_profile):
    IstemUserLink.objects.create(user=UserFactory(), public_user_id="60016", state=UserLinkState.LINKED)
    _, link = _run_duplicate(otp_on, {"result": "ERROR", "message": "EmailId already exists", "data": [{"user_id": "60016"}]})
    assert link.state == UserLinkState.NEEDS_REVIEW
    assert "another portal account" in link.review_reason


# --- send and verify --------------------------------------------------------------------------------------------

def test_correct_otp_links_at_once_and_requeues_parked_bookings(otp_on, mailoutbox, django_capture_on_commit_callbacks):
    booking = _booking_with_unlinked_user()
    link = _awaiting(booking.user)
    IstemBookingLink.objects.create(booking=booking, user_link=link, sync_state=SyncState.USER_PENDING)
    sent = otp_link.send_otp(booking.user)
    assert sent["email_masked"] == "a***@example.com"
    assert mailoutbox[-1].to == [ISTEM_EMAIL]
    otp = IstemLinkOtp.objects.get()
    assert _code(mailoutbox) not in otp.code_hash  # only an HMAC is stored
    with django_capture_on_commit_callbacks(execute=True):
        otp_link.verify_otp(booking.user, " ".join(_code(mailoutbox)))  # spaces are ignored
    link.refresh_from_db()
    assert (link.state, link.public_user_id) == (UserLinkState.LINKED, "60016")
    assert (link.candidate_public_user_id, link.candidate_email) == ("", "")
    assert IstemLinkOtp.objects.get().state == LinkOtpState.VERIFIED
    event = IstemSyncEvent.objects.filter(user_link=link, to_state=UserLinkState.LINKED).get()
    assert event.evidence_type == EvidenceType.EMAIL_OTP
    assert IstemOutboxJob.objects.filter(operation=Operation.CREATE_BOOKING, state=JobState.PENDING).exists()


def test_wrong_codes_count_down_then_use_up_the_otp(otp_on, mailoutbox):
    link = _awaiting()
    otp_link.send_otp(link.user)
    code = _code(mailoutbox)
    for left in (4, 3, 2, 1):
        with pytest.raises(OtpLinkError) as exc:
            otp_link.verify_otp(link.user, _wrong(code))
        assert (exc.value.code, exc.value.extra["attempts_left"]) == ("wrong_code", left)
    with pytest.raises(OtpLinkError, match="Too many incorrect codes"):
        otp_link.verify_otp(link.user, _wrong(code))
    with pytest.raises(OtpLinkError) as exc:
        otp_link.verify_otp(link.user, code)  # the right code no longer works
    assert exc.value.code == "no_otp"
    assert IstemLinkOtp.objects.get().state == LinkOtpState.EXHAUSTED
    assert IstemSyncEvent.objects.filter(user_link=link, reason__startswith="wrong email OTP").count() == 5
    link.refresh_from_db()
    assert link.state == UserLinkState.AWAITING_OTP


def test_repeated_failures_pause_linking(otp_on, mailoutbox):
    link = _awaiting()
    for _ in range(otp_link.PAUSE_AFTER_EXHAUSTED):
        IstemLinkOtp.objects.update(sent_at=timezone.now() - timedelta(minutes=5))  # past the resend interval
        otp_link.send_otp(link.user)
        for _ in range(otp_link.MAX_ATTEMPTS):
            with pytest.raises(OtpLinkError):
                otp_link.verify_otp(link.user, _wrong(_code(mailoutbox)))
    link.refresh_from_db()
    assert link.otp_paused_until > timezone.now()
    with pytest.raises(OtpLinkError) as exc:
        otp_link.send_otp(link.user)
    assert (exc.value.code, exc.value.http_status) == ("paused", 429)


def test_expired_otp_is_refused(otp_on, mailoutbox):
    link = _awaiting()
    otp_link.send_otp(link.user)
    IstemLinkOtp.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(OtpLinkError) as exc:
        otp_link.verify_otp(link.user, _code(mailoutbox))
    assert exc.value.code == "expired"
    assert IstemLinkOtp.objects.get().state == LinkOtpState.EXPIRED


def test_resend_is_spaced_capped_and_replaces_the_earlier_code(otp_on, mailoutbox):
    link = _awaiting()
    otp_link.send_otp(link.user)
    first = _code(mailoutbox)
    with pytest.raises(OtpLinkError) as exc:
        otp_link.send_otp(link.user)
    assert exc.value.code == "too_soon"
    for _ in range(otp_link.MAX_SENDS_PER_HOUR - 1):
        IstemLinkOtp.objects.update(sent_at=timezone.now() - timedelta(minutes=2))
        otp_link.send_otp(link.user)
    assert IstemLinkOtp.objects.filter(state=LinkOtpState.SUPERSEDED).count() == otp_link.MAX_SENDS_PER_HOUR - 1
    IstemLinkOtp.objects.update(sent_at=timezone.now() - timedelta(minutes=2))
    with pytest.raises(OtpLinkError) as exc:
        otp_link.send_otp(link.user)
    assert exc.value.code == "rate_limited"
    latest = _code(mailoutbox)
    if latest != first:
        with pytest.raises(OtpLinkError):
            otp_link.verify_otp(link.user, first)  # an earlier code does not work


def test_istem_user_linked_elsewhere_before_verify_is_refused(otp_on, mailoutbox):
    link = _awaiting()
    otp_link.send_otp(link.user)
    IstemUserLink.objects.create(user=UserFactory(), public_user_id="60016", state=UserLinkState.LINKED)
    with pytest.raises(OtpLinkError) as exc:
        otp_link.verify_otp(link.user, _code(mailoutbox))
    assert exc.value.code == "linked_elsewhere"
    link.refresh_from_db()
    assert link.state == UserLinkState.NEEDS_REVIEW
    assert IstemLinkOtp.objects.get().state == LinkOtpState.REFUSED


def test_istem_user_linked_elsewhere_gets_no_otp(otp_on, mailoutbox):
    link = _awaiting()
    IstemUserLink.objects.create(user=UserFactory(), public_user_id="60016", state=UserLinkState.LINKED)
    with pytest.raises(OtpLinkError) as exc:
        otp_link.send_otp(link.user)
    assert exc.value.code == "linked_elsewhere"
    assert mailoutbox == []
    link.refresh_from_db()
    assert link.state == UserLinkState.NEEDS_REVIEW


def test_failed_email_records_no_otp(otp_on, monkeypatch):
    link = _awaiting()

    def fail(*args, **kwargs):
        raise OSError("smtp down")

    monkeypatch.setattr(otp_link, "send_mail", fail)
    with pytest.raises(OtpLinkError) as exc:
        otp_link.send_otp(link.user)
    assert exc.value.code == "send_failed"
    assert not IstemLinkOtp.objects.exists()


def test_two_linked_accounts_on_one_istem_user_are_refused_by_the_database():
    IstemUserLink.objects.create(user=UserFactory(), public_user_id="60016", state=UserLinkState.LINKED)
    with pytest.raises(IntegrityError), transaction.atomic():
        IstemUserLink.objects.create(user=UserFactory(), public_user_id="60016", state=UserLinkState.LINKED)


def test_otp_linking_is_off_by_default(live):
    link = _awaiting()
    with pytest.raises(OtpLinkError) as exc:
        otp_link.send_otp(link.user)
    assert (exc.value.code, exc.value.http_status) == ("disabled", 403)
    assert otp_link.link_status(link.user)["otp_required"] is False


# --- API --------------------------------------------------------------------------------------------------------

def test_api_status_send_and_verify(otp_on, mailoutbox):
    link = _awaiting()
    api = APIClient()
    api.force_authenticate(link.user)
    status = api.get("/api/istem/link/").json()
    assert (status["otp_required"], status["email_masked"]) == (True, "a***@example.com")
    sent = api.post("/api/istem/link/send-otp/")
    assert sent.status_code == 200
    assert api.post("/api/istem/link/send-otp/").json()["code"] == "too_soon"
    wrong = api.post("/api/istem/link/verify-otp/", {"otp": _wrong(_code(mailoutbox))}, format="json")
    assert (wrong.status_code, wrong.json()["code"]) == (400, "wrong_code")
    done = api.post("/api/istem/link/verify-otp/", {"otp": _code(mailoutbox)}, format="json")
    assert done.status_code == 200
    assert (done.json()["state"], done.json()["public_user_id"]) == (UserLinkState.LINKED, "60016")


def test_api_requires_sign_in_and_only_acts_on_own_link(otp_on):
    _awaiting()
    assert APIClient().get("/api/istem/link/").status_code in (401, 403)
    api = APIClient()
    api.force_authenticate(UserFactory())
    response = api.post("/api/istem/link/send-otp/")
    assert (response.status_code, response.json()["code"]) == (409, "not_awaiting_otp")
