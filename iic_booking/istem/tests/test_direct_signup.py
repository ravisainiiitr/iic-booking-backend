"""Signing up in one step with a verified I-STEM account, then completing the profile on the dashboard."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.istem import direct_signup
from iic_booking.istem import registration_lookup as reg
from iic_booking.istem.client import IstemClient, clear_token_cache
from iic_booking.istem.models import IstemUserLink
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

from .support import FakeTransport, apply_live, json_response, token_response

pytestmark = pytest.mark.django_db

ROW = {"user_id": "60018", "user_name": "PU_X", "user_firstname": " Neha ", "user_lastname": " Rao ",
       "user_email": "neha@lab.example.org", "user_contactno": "9876543210", "user_organisation": "Asha Institute of Science",
       "state": "Uttarakhand", "user_gender": "Female", "user_country": "India", "user_type": "Industry User",
       "user_status": "1"}


@pytest.fixture
def on(settings):
    apply_live(settings)
    settings.ISTEM_OTP_LINKING_ENABLED = True
    settings.ISTEM_REGISTRATION_TEST_KEY = ""
    cache.clear()
    return settings


def _factory(row=ROW):
    def make():
        clear_token_cache()
        return IstemClient(FakeTransport(token_response(), json_response({"status": "SUCCESS", "result": row})))
    return make


def _verified(mailoutbox, row=ROW):
    sent = reg.start(row["user_email"], "", client_factory=_factory(row))
    code = re.search(r"\b(\d{6})\b", mailoutbox[-1].body).group(1)
    return reg.verify(sent["token"], code)["link_token"]


def _org(name="Asha Institute of Science", state="uttarakhand", sub="educational_institute"):
    return Department.objects.create(name=name, code=name[:4].upper() + state[:3].upper(), department_type=DepartmentType.EXTERNAL,
                                     external_subcategory=sub, state=state)


def test_state_and_organisation_matching():
    assert direct_signup.match_state("Uttarakhand") == "uttarakhand"
    assert direct_signup.match_state("  jammu & kashmir ") == "jammu_kashmir"
    assert direct_signup.match_state("Atlantis") is None
    org = _org()
    assert direct_signup.match_department("ASHA institute of science.", "uttarakhand", "external") == org
    assert direct_signup.match_department("Asha Institute of Science", "delhi", "external") is None  # other state
    assert direct_signup.match_department("Asha Institute of Science", None, "external") == org
    assert direct_signup.match_department("Asha Institute of Science", "uttarakhand", "Industry") is None  # other list


def test_account_is_created_signed_in_and_linked_without_a_password(on, mailoutbox):
    org = _org()
    link_token = _verified(mailoutbox)
    res = APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": UserType.EXTERNAL},
                           format="json")
    assert res.status_code == 201, res.data
    user = User.objects.get(email="neha@lab.example.org")
    assert user.name == "Neha Rao" and user.gender == "female" and user.phone_number == "9876543210"
    assert user.department == org and user.is_active and user.admin_approved and user.email_verified
    assert not user.has_usable_password()
    assert IstemUserLink.objects.get(user=user).public_user_id == "60018"
    assert res.data["token"] and res.data["user"]["email"] == "neha@lab.example.org"
    assert res.data["onboarding"]["steps"] == ["programme", "password"]
    # Signed in: the token works.
    me = APIClient(HTTP_AUTHORIZATION=f"Token {res.data['token']}").get("/api/profiles/me/onboarding/")
    assert me.status_code == 200 and me.data["steps"] == ["programme", "password"]


def test_unmatched_organisation_is_asked_with_the_istem_hints(on, mailoutbox):
    link_token = _verified(mailoutbox)
    res = APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": UserType.EXTERNAL},
                           format="json")
    onboarding = res.data["onboarding"]
    assert onboarding["steps"][0] == "organisation"
    assert onboarding["hints"] == {"state": "uttarakhand", "organisation": "Asha Institute of Science",
                                   "subcategory": "educational_institute"}


def test_refusals(on, mailoutbox):
    assert APIClient().post("/api/istem/register/complete/", {"link_token": "forged", "user_type": "external"},
                            format="json").status_code == 400
    link_token = _verified(mailoutbox)
    assert APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": UserType.STUDENT},
                            format="json").data["code"] == "invalid_user_type"
    User.objects.create_user(email="neha@lab.example.org", password="x-Strong-1!", user_type=UserType.EXTERNAL)
    assert APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": "external"},
                            format="json").status_code == 409


def test_unavailable_when_istem_is_off(on, mailoutbox):
    link_token = _verified(mailoutbox)
    on.ISTEM_OTP_LINKING_ENABLED = False
    assert APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": "external"},
                            format="json").status_code == 403


def test_onboarding_steps_one_by_one_then_password_sign_in(on, mailoutbox):
    link_token = _verified(mailoutbox)
    token = APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": UserType.EXTERNAL},
                             format="json").data["token"]
    api = APIClient(HTTP_AUTHORIZATION=f"Token {token}")
    url = "/api/profiles/me/onboarding/"
    assert api.post(url, {"step": "organisation", "department": 999999}, format="json").status_code == 400
    org = _org()
    assert api.post(url, {"step": "organisation", "department": org.pk}, format="json").data["steps"] == ["programme", "password"]
    past = (timezone.localdate() - timedelta(days=1)).isoformat()
    assert api.post(url, {"step": "programme", "program_end_date": past}, format="json").status_code == 400
    end = (timezone.localdate() + timedelta(days=200)).isoformat()
    assert api.post(url, {"step": "programme", "program_end_date": end}, format="json").data["steps"] == ["password"]
    bad = api.post(url, {"step": "password", "password": "Str0ng-pass!", "password_confirm": "other"}, format="json")
    assert bad.status_code == 400 and bad.data["field"] == "password_confirm"
    done = api.post(url, {"step": "password", "password": "Str0ng-pass!x", "password_confirm": "Str0ng-pass!x"}, format="json")
    assert done.data == {**done.data, "required": False, "steps": []}
    login = APIClient().post("/api/auth/login/", {"email": "neha@lab.example.org", "password": "Str0ng-pass!x"}, format="json")
    assert login.status_code == 200, login.data


def test_internal_users_are_never_asked(on):
    student = User.objects.create_user(email="s@iitr.ac.in", password=None, user_type=UserType.STUDENT)
    res = APIClient()
    res.force_authenticate(student)
    assert res.get("/api/profiles/me/onboarding/").data["required"] is False
