"""Registering with an existing I-STEM account: look-up by email and mobile, OTP to the I-STEM email, then the new
portal account is linked (no network: a fake I-STEM transport)."""

from __future__ import annotations

import re

import pytest
from django.core.cache import cache
from rest_framework.test import APIClient

from iic_booking.istem import registration_lookup as reg
from iic_booking.istem.choices import UserLinkState
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.models import IstemUserLink
from iic_booking.users.tests.factories import UserFactory

from .support import FakeTransport, apply_live, json_response, token_response

pytestmark = pytest.mark.django_db

ROW = {"user_id": "22001", "user_name": "PU_ASHA001", "user_firstname": "Asha", "user_lastname": "Verma",
       "user_email": "asha@lab.example.com", "user_contactno": "9876543210", "user_organisation": "Asha Labs",
       "user_address": "12 Station Road", "user_city": "Roorkee", "state": "Uttarakhand", "user_pin": "247667",
       "user_country": "India", "user_type": "Academic User", "user_status": "1"}
FOUND = {"status": "SUCCESS", "message": "User found", "result": ROW}


@pytest.fixture
def on(settings):
    apply_live(settings)
    settings.ISTEM_OTP_LINKING_ENABLED = True
    cache.clear()
    return settings


def _client_factory(body=FOUND):
    def make():
        clear_token_cache()  # each look-up here starts a fresh fake I-STEM conversation (token, then look-up)
        return IstemClient(FakeTransport(token_response(), json_response(body)))
    return make


def _code(mailoutbox) -> str:
    return re.search(r"\b(\d{6})\b", mailoutbox[-1].body).group(1)


def test_unavailable_while_istem_is_off(settings):
    settings.ISTEM_ENABLED = False
    assert APIClient().get("/api/istem/register/availability/").json() == {"available": False}
    with pytest.raises(reg.RegistrationLookupError) as exc:
        reg.start("asha@lab.example.com", "9876543210")
    assert exc.value.code == "unavailable"


def test_lookup_needs_both_email_and_mobile_of_the_same_account(on, mailoutbox):
    with pytest.raises(reg.RegistrationLookupError) as exc:
        reg.start("asha@lab.example.com", "9999999999", client_factory=_client_factory())
    assert exc.value.code == "not_found" and not mailoutbox


def test_otp_then_profile_then_the_new_account_is_linked(on, mailoutbox):
    sent = reg.start("Asha@Lab.example.com", "+91 98765 43210", client_factory=_client_factory())
    assert sent["email_masked"] == "a***@lab.example.com" and mailoutbox[-1].to == ["asha@lab.example.com"]
    with pytest.raises(reg.RegistrationLookupError) as exc:
        reg.verify(sent["token"], "000000" if _code(mailoutbox) != "000000" else "111111")
    assert exc.value.code == "wrong_code"
    result = reg.verify(sent["token"], _code(mailoutbox))
    assert result["profile"]["organisation"] == "Asha Labs" and result["profile"]["first_name"] == "Asha"
    with pytest.raises(reg.RegistrationLookupError):  # one use only
        reg.verify(sent["token"], _code(mailoutbox))

    user = UserFactory(email="asha@lab.example.com")
    assert reg.link_new_user(user, result["link_token"]) is True
    link = IstemUserLink.objects.get(user=user)
    assert (link.state, link.public_user_id) == (UserLinkState.LINKED, "22001")
    with pytest.raises(reg.RegistrationLookupError) as exc:  # the I-STEM account is now taken
        reg.start("asha@lab.example.com", "9876543210", client_factory=_client_factory())
    assert exc.value.code == "already_registered"


def test_link_token_is_only_for_the_verified_email(on, mailoutbox):
    sent = reg.start("asha@lab.example.com", "9876543210", client_factory=_client_factory())
    result = reg.verify(sent["token"], _code(mailoutbox))
    other = UserFactory(email="someone.else@example.com")
    assert reg.link_new_user(other, result["link_token"]) is False
    assert reg.link_new_user(other, "forged") is False
    assert not IstemUserLink.objects.filter(user=other).exists()


def test_too_many_wrong_codes_end_the_lookup(on, mailoutbox):
    sent = reg.start("asha@lab.example.com", "9876543210", client_factory=_client_factory())
    wrong = "000000" if _code(mailoutbox) != "000000" else "111111"
    for _ in range(reg.MAX_ATTEMPTS - 1):
        with pytest.raises(reg.RegistrationLookupError):
            reg.verify(sent["token"], wrong)
    with pytest.raises(reg.RegistrationLookupError) as exc:
        reg.verify(sent["token"], wrong)
    assert exc.value.code == "attempts_exceeded"
    with pytest.raises(reg.RegistrationLookupError) as exc:
        reg.verify(sent["token"], _code(mailoutbox))
    assert exc.value.code == "expired"
