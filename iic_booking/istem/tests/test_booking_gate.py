"""A booking on an I-STEM instrument stops before payment when the user's profile lacks what I-STEM needs (8 Nov 2026).
Test accounts first (ISTEM_BOOKING_GATE=test_accounts); a missing GSTIN never blocks (the placeholder is sent)."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.istem.booking_gate import refusal
from iic_booking.users.models import UserType
from iic_booking.users.models.billing import ExternalBillingProfile
from iic_booking.users.tests.factories import UserFactory

from .support import apply_live, link_user, make_booking, map_equipment

pytestmark = pytest.mark.django_db


@pytest.fixture
def automatic(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True, ISTEM_BOOKING_GATE="test_accounts")
    return settings


def _external(*, test=True):
    return UserFactory(user_type=UserType.EXTERNAL, name="Asha Verma", admin_approved=True, is_test_account=test)


def _setup(user, *, with_billing=True, mapped=True, gstin="09AAAAA0000A1Z5"):
    booking = make_booking(user=user, with_billing=with_billing, gstin=gstin)
    if mapped:
        map_equipment(booking.equipment)
    return booking.equipment, booking.charge_profile


def test_an_unlinked_test_account_is_refused_with_what_to_complete(automatic):
    user = _external()
    equipment, profile = _setup(user)
    body = refusal(user, equipment, profile)
    assert body["code"] == "ISTEM_PROFILE_INCOMPLETE"
    assert [p["field"] for p in body["missing"]] == ["istem_link"]
    assert body["profile_url"] == "/profile"


def test_missing_billing_fields_are_listed(automatic):
    user = _external()
    link_user(user)
    equipment, profile = _setup(user)
    ExternalBillingProfile.objects.filter(user=user).update(billing_state="", billing_pincode="")
    user.refresh_from_db()
    fields = [p["field"] for p in refusal(user, equipment, profile)["missing"]]
    assert fields == ["billing_pincode", "billing_state"]


def test_no_billing_profile_at_all(automatic):
    user = _external()
    link_user(user)
    equipment, profile = _setup(user, with_billing=False)
    assert [p["field"] for p in refusal(user, equipment, profile)["missing"]] == ["billing"]


def test_an_empty_gstin_does_not_block(automatic):
    user = _external()
    link_user(user)
    equipment, profile = _setup(user, gstin="")
    assert refusal(user, equipment, profile) is None


def test_a_malformed_gstin_blocks(automatic):
    user = _external()
    link_user(user)
    equipment, profile = _setup(user, gstin="12AB")
    assert [p["field"] for p in refusal(user, equipment, profile)["missing"]] == ["gstin"]


def test_real_users_are_not_stopped_while_the_gate_is_for_test_accounts(automatic):
    user = _external(test=False)
    equipment, profile = _setup(user, with_billing=False)
    assert refusal(user, equipment, profile) is None
    automatic.ISTEM_BOOKING_GATE = "all"
    assert refusal(user, equipment, profile) is not None
    automatic.ISTEM_BOOKING_GATE = "off"
    user2 = _external()
    equipment, profile = _setup(user2, with_billing=False)
    assert refusal(user2, equipment, profile) is None


def test_instruments_not_on_istem_and_manual_mode_never_block(automatic):
    user = _external()
    equipment, profile = _setup(user, mapped=False)
    assert refusal(user, equipment, profile) is None  # the IIC team's setup, not the user's
    equipment, profile = _setup(user)
    automatic.ISTEM_AUTOMATIC_FBR = False
    assert refusal(user, equipment, profile) is None


def test_internal_users_need_only_the_link(automatic):
    user = UserFactory(user_type=UserType.STUDENT, name="Ravi Kumar", admin_approved=True, is_test_account=True)
    equipment, profile = _setup(user, with_billing=False)
    assert [p["field"] for p in refusal(user, equipment, profile)["missing"]] == ["istem_link"]
    link_user(user)
    assert refusal(user, equipment, profile) is None


def test_readiness_endpoint(automatic):
    user = _external()
    equipment, _ = _setup(user, with_billing=False)
    client = APIClient()
    client.force_authenticate(user)
    body = client.get("/api/istem/profile-readiness/", {"equipment_id": equipment.pk}).json()
    assert body["complete"] is False and body["blocks_booking"] is True
    assert {p["field"] for p in body["missing"]} == {"istem_link", "billing"}
    assert client.get("/api/istem/profile-readiness/").json()["blocks_booking"] is False
