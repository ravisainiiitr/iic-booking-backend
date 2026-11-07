"""One-time codes on WhatsApp: the authentication template, the gates, proving a number, sign-in and I-STEM codes. No network."""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from iic_booking.istem import registration_lookup as reg
from iic_booking.istem.choices import EvidenceType
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.models import IstemSyncEvent, IstemUserLink
from iic_booking.istem.tests.support import FakeTransport, apply_live, json_response, token_response
from iic_booking.istem.tests.test_registration_lookup import ROW
from iic_booking.users.tests.factories import UserFactory
from iic_booking.whatsapp import otp, service, templates
from iic_booking.whatsapp.client import WhatsAppError, send_code_template
from iic_booking.whatsapp.models import WhatsAppConsentEvent, WhatsAppMessage, WhatsAppOptIn, WhatsAppTemplate
from iic_booking.whatsapp.template_catalogue import BY_EVENT, validate

pytestmark = pytest.mark.django_db

PHONE = "+919876543210"
WABA = "3000000000000333"
ENTRY = BY_EVENT["otp_code"]


class Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code, self._body = status_code, {"messages": [{"id": "wamid.unit"}]} if body is None else body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses) or [Resp()], []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


@pytest.fixture
def on(settings):
    cache.clear()
    settings.WHATSAPP_WABA_ID = WABA
    settings.WHATSAPP_ENABLED = True
    settings.WHATSAPP_PHONE_NUMBER_ID = "1234567890"
    settings.WHATSAPP_ACCESS_TOKEN = "unit-test-token-not-real"
    settings.WHATSAPP_APP_SECRET = "unit-test-secret-not-real"
    settings.WHATSAPP_VERIFY_TOKEN = "unit-test-verify-not-real"
    settings.WHATSAPP_PILOT_NUMBERS = [PHONE]
    WhatsAppTemplate.objects.create(waba_id=WABA, name=ENTRY.name, language=ENTRY.language, status="APPROVED")
    return settings


def _opted_in(user=None, *, verified=False):
    user = user or UserFactory()
    WhatsAppOptIn.objects.create(user=user, phone_e164=PHONE, opted_in=True, phone_verified_e164=PHONE if verified else "")
    return user


def _api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


# --- the template ---------------------------------------------------------------------------------------------------

def test_the_template_is_what_meta_requires_for_authentication():
    payload = ENTRY.payload()
    assert payload["category"] == "AUTHENTICATION" and payload["name"] == "iitr_verification_code"
    kinds = {c["type"]: c for c in payload["components"]}
    assert kinds["BODY"] == {"type": "BODY", "add_security_recommendation": True}
    assert kinds["FOOTER"]["code_expiration_minutes"] == 10
    assert kinds["BUTTONS"]["buttons"] == [{"type": "OTP", "otp_type": "COPY_CODE", "text": "Copy code"}]
    assert not validate()


def test_metas_own_wording_is_not_reported_as_a_difference():
    row = {"name": ENTRY.name, "language": "en", "components": [{"type": "BODY", "text": "*{{1}}* is your verification code."}]}
    assert templates.wording_differs(ENTRY, row) is False


def test_the_message_carries_the_code_in_the_body_and_on_the_button(on):
    session = FakeSession()
    assert send_code_template(PHONE, ENTRY.name, "482913", language="en", session=session) == "wamid.unit"
    components = session.calls[0]["json"]["template"]["components"]
    assert components[0] == {"type": "body", "parameters": [{"type": "text", "text": "482913"}]}
    assert components[1] == {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": "482913"}]}
    assert session.calls[0]["timeout"] == (3, 6)


# --- the gates ------------------------------------------------------------------------------------------------------

def test_the_code_is_never_stored(on):
    user = _opted_in(verified=True)
    assert otp.send_sign_in_code(user, "482913", session=FakeSession()) == "number ending 10"
    msg = WhatsAppMessage.objects.get()
    assert msg.params == [] and msg.status == "sent" and msg.event == "otp_code"
    assert "482913" not in f"{msg.event_key} {msg.error} {msg.params}"


def test_metas_error_text_cannot_keep_the_code(on):
    user = _opted_in(verified=True)
    session = FakeSession(Resp(400, {"error": {"code": 131026, "message": "cannot deliver 482913"}}))
    assert otp.send_sign_in_code(user, "482913", session=session) == ""
    msg = WhatsAppMessage.objects.get()
    assert msg.status == "failed" and "482913" not in msg.error and "[code]" in msg.error


@pytest.mark.parametrize("change", ["switched_off", "not_approved", "outside_pilot", "no_waba"])
def test_nothing_goes_out_unless_every_gate_is_open(on, change):
    if change == "switched_off":
        on.WHATSAPP_ENABLED = False
    elif change == "not_approved":
        WhatsAppTemplate.objects.update(status="PENDING")
    elif change == "outside_pilot":
        on.WHATSAPP_PILOT_NUMBERS = ["+919000000000"]
    else:
        on.WHATSAPP_WABA_ID = ""
    session = FakeSession()
    assert otp.send_sign_in_code(_opted_in(verified=True), "482913", session=session) == ""
    assert session.calls == []


def test_sends_to_one_number_are_limited(on):
    user, session = _opted_in(verified=True), FakeSession()
    sent = [otp.send_sign_in_code(user, "482913", session=session) for _ in range(otp.SENDS_PER_NUMBER + 2)]
    assert sum(bool(s) for s in sent) == otp.SENDS_PER_NUMBER and len(session.calls) == otp.SENDS_PER_NUMBER


def test_a_failure_never_reaches_the_caller(on):
    user = _opted_in(verified=True)
    with patch("iic_booking.whatsapp.otp.send_code_template", side_effect=RuntimeError("boom")):
        assert otp.send_sign_in_code(user, "482913") == ""
    with patch("iic_booking.whatsapp.otp.send_code_template", side_effect=WhatsAppError("down", retryable=True)):
        assert otp.send_sign_in_code(user, "482913") == ""


# --- proving a number -----------------------------------------------------------------------------------------------

def _sent_code(session) -> str:
    return session.calls[-1]["json"]["template"]["components"][0]["parameters"][0]["text"]


def test_a_number_is_proved_with_a_code(on):
    user, session = _opted_in(), FakeSession()
    assert otp.sign_in_number(user) == ""
    assert otp.start_verification(user, session=session)["sent_to"] == "number ending 10"
    otp.confirm_verification(user, _sent_code(session))
    assert otp.sign_in_number(user) == PHONE
    assert service.opt_in_status(user)["phone_verified"] is True


def test_the_api_proves_a_number(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.otp.send_code_template", return_value="wamid.unit") as send:
        assert _api(user).post("/api/whatsapp/me/verify/send/").status_code == 200
    code = send.call_args.args[2]
    answer = _api(user).post("/api/whatsapp/me/verify/confirm/", {"otp": code}, format="json")
    assert answer.status_code == 200 and answer.json()["phone_verified"] is True


def test_proving_needs_the_opt_in_first(on):
    answer = _api(UserFactory()).post("/api/whatsapp/me/verify/send/")
    assert answer.status_code == 400 and answer.json()["code"] == "not_opted_in"


def test_wrong_codes_are_limited(on):
    user, session = _opted_in(), FakeSession()
    otp.start_verification(user, session=session)
    wrong = f"{(int(_sent_code(session)) + 1) % 10 ** 6:06d}"
    for _ in range(otp.VERIFY_MAX_ATTEMPTS - 1):
        with pytest.raises(otp.VerificationError) as exc:
            otp.confirm_verification(user, wrong)
        assert exc.value.code == "wrong_code"
    with pytest.raises(otp.VerificationError) as exc:
        otp.confirm_verification(user, wrong)
    assert exc.value.code == "attempts_exceeded"
    with pytest.raises(otp.VerificationError) as exc:
        otp.confirm_verification(user, _sent_code(session))
    assert exc.value.code == "expired" and otp.sign_in_number(user) == ""


def test_changing_the_number_undoes_the_proof(on):
    user = _opted_in(verified=True)
    assert otp.sign_in_number(user) == PHONE
    service.set_opt_in(user, opted_in=True, phone="+919000000000")
    assert otp.sign_in_number(user) == "" and service.opt_in_status(user)["phone_verified"] is False


def test_a_code_for_the_old_number_cannot_prove_the_new_one(on):
    user, session = _opted_in(), FakeSession()
    otp.start_verification(user, session=session)
    service.set_opt_in(user, opted_in=True, phone="+919000000000")
    with pytest.raises(otp.VerificationError) as exc:
        otp.confirm_verification(user, _sent_code(session))
    assert exc.value.code == "number_changed"


def test_stop_ends_sign_in_codes(on):
    user = _opted_in(verified=True)
    service.set_opt_in(user, opted_in=False)
    assert otp.sign_in_number(user) == ""


# --- sign-in --------------------------------------------------------------------------------------------------------

def _login_user():
    return UserFactory(is_active=True, email_verified=True, admin_approved=True, email_login_enabled=True)


def _request_login(user):
    return APIClient().post("/api/auth/login/request-otp/", {"email": user.email}, format="json")


def test_sign_in_code_also_goes_to_a_proved_number_and_works(on, mailoutbox):
    user = _opted_in(_login_user(), verified=True)
    with patch("iic_booking.whatsapp.otp.send_code_template", return_value="wamid.unit") as send:
        answer = _request_login(user)
    assert answer.status_code == 200 and "WhatsApp (number ending 10)" in answer.json()["message"] and mailoutbox
    code = send.call_args.args[2]
    assert re.search(r"\b(\d{6})\b", mailoutbox[-1].body).group(1) == code
    done = APIClient().post("/api/auth/login/verify-otp/", {"email": user.email, "otp": code}, format="json")
    assert done.status_code == 200


def test_an_unproved_number_never_gets_a_sign_in_code(on, mailoutbox):
    user = _opted_in(_login_user())
    with patch("iic_booking.whatsapp.otp.send_code_template") as send:
        answer = _request_login(user)
    assert answer.status_code == 200 and not send.called and "WhatsApp" not in answer.json()["message"] and mailoutbox


def test_the_profile_phone_alone_never_gets_a_sign_in_code(on, mailoutbox):
    user = _login_user()
    user.phone_number = "9876543210"
    user.save(update_fields=["phone_number"])
    with patch("iic_booking.whatsapp.otp.send_code_template") as send:
        assert _request_login(user).status_code == 200
    assert not send.called and mailoutbox


def test_sign_in_is_unchanged_when_whatsapp_fails(on, mailoutbox):
    user = _opted_in(_login_user(), verified=True)
    with patch("iic_booking.whatsapp.otp.send_code_template", side_effect=WhatsAppError("refused")):
        answer = _request_login(user)
    assert answer.status_code == 200 and answer.json()["message"].startswith("OTP has been sent to your email address.") and mailoutbox


# --- I-STEM registration --------------------------------------------------------------------------------------------

def _istem_factory():
    def make():
        clear_token_cache()
        return IstemClient(FakeTransport(token_response(), json_response({"status": "SUCCESS", "message": "User found", "result": ROW})))
    return make


@pytest.fixture
def istem_on(on):
    apply_live(on)
    on.ISTEM_OTP_LINKING_ENABLED = True
    on.ISTEM_WHATSAPP_OTP_ENABLED = True
    return on


def _email_code(mailoutbox) -> str:
    return re.search(r"\b(\d{6})\b", mailoutbox[-1].body).group(1)


def test_registration_stays_email_only_until_it_is_switched_on(istem_on, mailoutbox):
    istem_on.ISTEM_WHATSAPP_OTP_ENABLED = False
    with patch("iic_booking.whatsapp.otp.send_code_template") as send:
        sent = reg.start("asha@lab.example.com", "", client_factory=_istem_factory())
    assert sent["whatsapp_masked"] == "" and not send.called and mailoutbox


def test_the_same_code_goes_to_email_and_whatsapp_and_works(istem_on, mailoutbox):
    with patch("iic_booking.whatsapp.otp.send_code_template", return_value="wamid.unit") as send:
        sent = reg.start("asha@lab.example.com", "", client_factory=_istem_factory())
    assert sent["whatsapp_masked"] == "number ending 10"
    code = _email_code(mailoutbox)
    assert send.call_args.args[2] == code
    assert WhatsAppConsentEvent.objects.filter(phone_e164=PHONE, source="otp_request", user__isnull=True).exists()
    result = reg.verify(sent["token"], code)
    user = UserFactory(email="asha@lab.example.com")
    assert reg.link_new_user(user, result["link_token"]) is True
    link = IstemUserLink.objects.get(user=user)
    assert link.public_user_id == ROW["user_id"]
    assert IstemSyncEvent.objects.filter(user_link=link, evidence_type=EvidenceType.EMAIL_OR_WHATSAPP_OTP).exists()


def test_when_whatsapp_fails_the_email_still_works_and_the_record_says_email(istem_on, mailoutbox):
    with patch("iic_booking.whatsapp.otp.send_code_template", side_effect=WhatsAppError("refused")):
        sent = reg.start("asha@lab.example.com", "", client_factory=_istem_factory())
    assert sent["whatsapp_masked"] == ""
    result = reg.verify(sent["token"], _email_code(mailoutbox))
    user = UserFactory(email="asha@lab.example.com")
    assert reg.link_new_user(user, result["link_token"]) is True
    assert IstemSyncEvent.objects.filter(user_link__user=user, evidence_type=EvidenceType.EMAIL_OTP).exists()


def test_no_mobile_on_istem_means_email_only(istem_on, mailoutbox):
    def factory():
        clear_token_cache()
        row = {**ROW, "user_contactno": ""}
        return IstemClient(FakeTransport(token_response(), json_response({"status": "SUCCESS", "message": "User found", "result": row})))

    with patch("iic_booking.whatsapp.otp.send_code_template") as send:
        sent = reg.start("asha@lab.example.com", "", client_factory=factory)
    assert sent["whatsapp_masked"] == "" and not send.called and mailoutbox


def test_someone_who_replied_stop_gets_no_code_on_whatsapp(istem_on, mailoutbox):
    WhatsAppOptIn.objects.create(user=UserFactory(), phone_e164=PHONE, opted_in=False, opted_out_at="2026-11-01T00:00:00Z")
    with patch("iic_booking.whatsapp.otp.send_code_template") as send:
        sent = reg.start("asha@lab.example.com", "", client_factory=_istem_factory())
    assert sent["whatsapp_masked"] == "" and not send.called and mailoutbox
