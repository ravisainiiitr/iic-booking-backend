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


# --- 31 Oct 2026: the email or the mobile number is enough; registration is confirmed at once ---------------------

def _rows_factory(body=FOUND):
    """Fake transport that answers the look-up whether it was asked by email or by mobile number."""
    return _client_factory(body)


def test_email_alone_is_enough(on, mailoutbox):
    sent = reg.start("asha@lab.example.com", "", client_factory=_rows_factory())
    assert mailoutbox[-1].to == ["asha@lab.example.com"] and sent["email_masked"] == "a***@lab.example.com"


def test_mobile_alone_sends_the_otp_to_the_email_registered_on_istem(on, mailoutbox):
    sent = reg.start("", "+91 98765 43210", client_factory=_rows_factory())
    assert mailoutbox[-1].to == ["asha@lab.example.com"]
    assert sent["email_masked"] == "a***@lab.example.com"


def test_neither_email_nor_mobile_is_refused(on):
    with pytest.raises(reg.RegistrationLookupError) as exc:
        reg.start("", "", client_factory=_rows_factory())
    assert exc.value.code == "invalid"


def test_registering_with_a_verified_istem_account_is_confirmed_at_once(on, mailoutbox):
    from django.utils import timezone

    from iic_booking.users.models import Department, DepartmentType, User
    from iic_booking.users.models.user_type import UserType

    sent = reg.start("", "9876543210", client_factory=_rows_factory())
    result = reg.verify(sent["token"], _code(mailoutbox))
    outbox_before = len(mailoutbox)
    org = Department.objects.create(name="Asha Labs", code="ASHA", department_type=DepartmentType.EXTERNAL)
    payload = {
        "email": "asha@lab.example.com", "password": "Str0ng-pass!", "password_confirm": "Str0ng-pass!",
        "name": "Asha Verma", "gender": "female", "phone_number": "9876543210", "user_type": UserType.EXTERNAL,
        "department": org.pk, "program_end_date": (timezone.localdate() + timezone.timedelta(days=200)).isoformat(),
        "istem_link_token": result["link_token"],
    }
    res = APIClient().post("/api/auth/register/", payload)
    assert res.status_code == 201, res.data
    assert res.data["registration_confirmed"] is True and res.data["istem_linked"] is True
    user = User.objects.get(email="asha@lab.example.com")
    assert user.email_verified and user.admin_approved and user.is_active
    assert IstemUserLink.objects.get(user=user).public_user_id == "22001"
    assert not any("self-verify" in m.body for m in mailoutbox[outbox_before:])  # no second verification email
    login = APIClient().post("/api/auth/login/", {"email": "asha@lab.example.com", "password": "Str0ng-pass!"},
                             format="json")
    assert login.status_code == 200, login.data


def test_without_a_valid_istem_token_registration_keeps_the_usual_email_step(on):
    from django.utils import timezone

    from iic_booking.users.models import Department, DepartmentType, User
    from iic_booking.users.models.user_type import UserType

    org = Department.objects.create(name="Other Labs", code="OTHR", department_type=DepartmentType.EXTERNAL)
    res = APIClient().post("/api/auth/register/", {
        "email": "other@lab.example.com", "password": "Str0ng-pass!", "password_confirm": "Str0ng-pass!",
        "name": "Other Person", "gender": "male", "phone_number": "9876500000", "user_type": UserType.EXTERNAL,
        "department": org.pk, "program_end_date": (timezone.localdate() + timezone.timedelta(days=200)).isoformat(),
        "istem_link_token": "forged",
    })
    assert res.status_code == 201, res.data
    assert "registration_confirmed" not in res.data
    user = User.objects.get(email="other@lab.example.com")
    assert not user.email_verified and not user.admin_approved
