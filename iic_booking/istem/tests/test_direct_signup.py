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
    assert res.data["onboarding"]["steps"] == ["password"]
    # Signed in: the token works.
    me = APIClient(HTTP_AUTHORIZATION=f"Token {res.data['token']}").get("/api/profiles/me/onboarding/")
    assert me.status_code == 200 and me.data["steps"] == ["password"]


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
    # An account already using the email but still awaiting approval: linked, not signed in.
    User.objects.create_user(email="neha@lab.example.org", password="x-Strong-1!", user_type=UserType.EXTERNAL,
                             is_active=False, admin_approved=False)
    res = APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": "external"},
                           format="json")
    assert res.status_code == 409 and res.data["code"] == "existing_pending"
    assert IstemUserLink.objects.get(user__email="neha@lab.example.org").public_user_id == "60018"


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
    assert api.post(url, {"step": "organisation", "department": org.pk}, format="json").data["steps"] == ["password"]
    past = (timezone.localdate() - timedelta(days=1)).isoformat()
    assert api.post(url, {"step": "programme", "program_end_date": past}, format="json").status_code == 400
    end = (timezone.localdate() + timedelta(days=200)).isoformat()
    assert api.post(url, {"step": "programme", "program_end_date": end}, format="json").data["steps"] == ["password"]
    bad = api.post(url, {"step": "password", "password": "Str0ng-pass!", "password_confirm": "other"}, format="json")
    assert bad.status_code == 400 and bad.data["error_field"] == "password_confirm" and "field" not in bad.data
    done = api.post(url, {"step": "password", "password": "Str0ng-pass!x", "password_confirm": "Str0ng-pass!x"}, format="json")
    assert done.data == {**done.data, "required": False, "steps": []}
    login = APIClient().post("/api/auth/login/", {"email": "neha@lab.example.org", "password": "Str0ng-pass!x"}, format="json")
    assert login.status_code == 200, login.data


def _admin_api():
    admin = User.objects.create_user(email="admin@iitr.ac.in", password="x-Strong-1!", user_type=UserType.ADMIN,
                                     is_active=True)
    api = APIClient()
    api.force_authenticate(admin)
    return api


def test_unlisted_organisation_is_added_at_once_and_marked_unverified(on, mailoutbox):
    from iic_booking.users import onboarding
    from iic_booking.users.models import OrganizationRequest

    link_token = _verified(mailoutbox)
    token = APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": UserType.EXTERNAL},
                             format="json").data["token"]
    api = APIClient(HTTP_AUTHORIZATION=f"Token {token}")
    url = "/api/profiles/me/onboarding/"
    short = api.post(url, {"step": "organisation", "request_organisation": True, "organisation_name": "AI",
                           "state": "uttarakhand"}, format="json")
    assert short.status_code == 400 and short.data["error_field"] == "organisation_name"
    res = api.post(url, {"step": "organisation", "request_organisation": True,
                         "organisation_name": "  Asha  Institute of Science ", "state": "uttarakhand"}, format="json")
    assert res.status_code == 200, res.data
    assert "organisation" not in res.data["steps"] and res.data["organisation_added"] == "Asha Institute of Science"
    user = User.objects.get(email="neha@lab.example.org")
    dept = user.department
    assert (dept.name, dept.state, dept.external_subcategory, dept.department_type) == (
        "Asha Institute of Science", "uttarakhand", "educational_institute", DepartmentType.EXTERNAL)
    req = OrganizationRequest.objects.get()
    assert req.status == OrganizationRequest.Status.PENDING and req.created_department == dept
    # Listed for others, marked unverified, and not twice.
    listed = APIClient().get("/api/departments/", {"type": "external", "external_subcategory": "educational_institute",
                                                   "state": "uttarakhand"}).data
    assert [(d["name"], d["verified"]) for d in listed["departments"]] == [("Asha Institute of Science", False)]
    # A second user typing the same name joins it rather than adding it again.
    other = User.objects.create_user(email="other@lab.example.org", password=None, user_type=UserType.EXTERNAL)
    onboarding.save_step(other, "organisation", {"request_organisation": True,
                                                 "organisation_name": "asha institute of science", "state": "uttarakhand"})
    other.refresh_from_db()
    assert other.department == dept and OrganizationRequest.objects.count() == 1


def _added(name="Asha Instt of Sci", email="neha@lab.example.org"):
    from iic_booking.users import provisional_organisations as provisional
    from iic_booking.users.models import OrganizationRequest

    user = User.objects.create_user(email=email, password=None, user_type=UserType.EXTERNAL)
    dept = provisional.add_for_user(user, name, "uttarakhand", "educational_institute")
    return user, dept, OrganizationRequest.objects.get(created_department=dept)


def test_admin_approves_an_added_organisation_with_a_corrected_name():
    user, dept, req = _added()
    _org(name="Taken Name")
    api = _admin_api()
    url = f"/api/admin/organization-requests/{req.pk}/"
    api.patch(url, {"approved_name": "Taken Name"}, format="json")
    assert api.post(url + "approve/").status_code == 400  # would duplicate a listed organisation
    api.patch(url, {"approved_name": "Asha Institute of Science"}, format="json")
    assert api.post(url + "approve/").status_code == 200
    dept.refresh_from_db()
    req.refresh_from_db()
    assert dept.name == "Asha Institute of Science" and req.status == "approved"
    user.refresh_from_db()
    assert user.department == dept


def test_admin_merges_a_duplicate_into_the_listed_organisation():
    from iic_booking.users.models import OrganizationRequest

    listed = _org()
    user, dept, req = _added()
    res = _admin_api().post(f"/api/admin/organization-requests/{req.pk}/merge/", {"department_id": listed.pk},
                            format="json")
    assert res.status_code == 200, res.data
    user.refresh_from_db()
    req.refresh_from_db()
    assert user.department == listed and not Department.objects.filter(pk=dept.pk).exists()
    assert req.status == OrganizationRequest.Status.APPROVED and req.created_department == listed


def test_admin_rejects_an_added_organisation_and_its_users_are_asked_again():
    from iic_booking.users import onboarding

    user, dept, req = _added()
    assert _admin_api().post(f"/api/admin/organization-requests/{req.pk}/reject/").status_code == 200
    user.refresh_from_db()
    assert user.department is None and not Department.objects.filter(pk=dept.pk).exists()
    assert "organisation" in onboarding.missing_steps(user)


def test_internal_users_are_never_asked(on):
    student = User.objects.create_user(email="s@iitr.ac.in", password=None, user_type=UserType.STUDENT)
    res = APIClient()
    res.force_authenticate(student)
    assert res.get("/api/profiles/me/onboarding/").data["required"] is False


def test_existing_portal_account_with_the_same_email_is_linked_and_signed_in(on, mailoutbox):
    org = _org()
    existing = User.objects.create_user(email="neha@lab.example.org", password="x-Strong-1!", user_type=UserType.EXTERNAL,
                                        department=org)
    User.objects.filter(pk=existing.pk).update(is_active=True, admin_approved=True, email_verified=True)
    link_token = _verified(mailoutbox)
    res = APIClient().post("/api/istem/register/complete/", {"link_token": link_token, "user_type": UserType.EXTERNAL},
                           format="json")
    assert res.status_code == 200, res.data
    assert res.data["outcome"] == "linked_existing" and res.data["token"]
    assert User.objects.filter(email__iexact="neha@lab.example.org").count() == 1
    existing.refresh_from_db()
    assert IstemUserLink.objects.get(user=existing).public_user_id == "60018"
    assert existing.is_test_account is False and existing.has_usable_password()


def test_internal_account_with_the_same_email_is_linked_but_signs_in_with_channel_i(on, mailoutbox):
    existing = User.objects.create_user(email="neha@lab.example.org", password=None, user_type=UserType.STUDENT)
    User.objects.filter(pk=existing.pk).update(is_active=True, admin_approved=True)
    res = APIClient().post("/api/istem/register/complete/", {"link_token": _verified(mailoutbox), "user_type": "external"},
                           format="json")
    assert res.status_code == 409 and res.data["code"] == "existing_internal"
    assert IstemUserLink.objects.get(user=existing).public_user_id == "60018"


def test_same_mobile_under_another_email_gets_no_second_account(on, mailoutbox):
    User.objects.create_user(email="neha.old@college.example.org", password="x-Strong-1!", user_type=UserType.EXTERNAL,
                             phone_number="9876543210")
    res = APIClient().post("/api/istem/register/complete/", {"link_token": _verified(mailoutbox), "user_type": "external"},
                           format="json")
    assert res.status_code == 409 and res.data["code"] == "mobile_exists"
    assert "neha@lab.example.org" not in res.data["error"]
    assert not User.objects.filter(email__iexact="neha@lab.example.org").exists()


def test_account_linked_to_another_istem_user_is_refused(on, mailoutbox):
    existing = User.objects.create_user(email="neha@lab.example.org", password="x-Strong-1!", user_type=UserType.EXTERNAL)
    User.objects.filter(pk=existing.pk).update(is_active=True, admin_approved=True)
    IstemUserLink.objects.create(user=existing, public_user_id="59999")
    res = APIClient().post("/api/istem/register/complete/", {"link_token": _verified(mailoutbox), "user_type": "external"},
                           format="json")
    assert res.status_code == 409 and res.data["code"] == "linked_other"
