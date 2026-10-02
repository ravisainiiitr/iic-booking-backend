"""A formula maximum (FE-SEM APREO: No. of Samples <= Number of Slots x 4) is worked out for every sample set
from that set's own values, on booking creation, charge calculation, edits, templates and the proforma."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.api_views import _sample_set_groups_limit_error
from iic_booking.equipment.calculators import SAMPLE_SETS_KEY
from iic_booking.equipment.models import (
    Booking,
    BookingInputTemplate,
    ChargeProfile,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory

APREO_USER_TYPES = ("external", "faculty", "Industry", "RND", "student")
B_LABEL = "Number of Slots ( Slot Duration: 1.5 Hours )"


def _apreo(f, a_options=None, b_help_text="", **kwargs):
    """Production FE-SEM APREO (probe-sample-set-formula, 2026-10-02): one row per user type,
    A "No. of Samples" options {"min": 1, "max_formula": "B*4"}; B options [] with no help text; C a radio."""
    eq = f.equipment(time_formula="B*90", slot_duration_minutes=90, **kwargs)
    for user_type in APREO_USER_TYPES:
        DynamicInputField.objects.create(
            equipment=eq, user_type=user_type, field_key="A", field_label="No. of Samples",
            field_type=DynamicInputFieldType.NUMERIC,
            options=a_options if a_options is not None else {"min": 1, "max_formula": "B*4"},
            default_value="1", is_required=True, editing_required=True,
        )
        DynamicInputField.objects.create(
            equipment=eq, user_type=user_type, field_key="B", field_label=B_LABEL,
            field_type=DynamicInputFieldType.NUMERIC, options=[], help_text=b_help_text,
            default_value="1", is_required=True, editing_required=True,
        )
        DynamicInputField.objects.create(
            equipment=eq, user_type=user_type, field_key="C",
            field_label="Do You want to Avail Gold Coating Facility?", field_type=DynamicInputFieldType.RADIO,
            options=["Yes", "No"], default_value="No",
        )
    return eq


def _formula_error(max_v, b, prefix="Sample set 2: "):
    return f"{prefix}No. of Samples cannot be greater than {max_v} (B × 4, where B is {B_LABEL} = {b})."


def _calc(client, eq, a, b, sets):
    return client.get(f"/api/equipments/{eq.pk}/calculate/", {"A": a, "B": b, "sample_sets": json.dumps(sets)})


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _student_with_wallet(f, balance="10000.00"):
    student = f.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=f.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED,
        responded_at=timezone.now() - timedelta(days=30),
    )
    SubWalletRepository.get_or_create(wallet, f.department).credit(Decimal(balance), description="Recharge")
    return student


def _book(f, user, eq, slot, input_values):
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": input_values,
        "waitlist_on_failure": False,
    }
    return f.client_for(user).post(f"/api/equipments/{eq.pk}/book/", body, format="json")


def _patch(f, user, booking, values):
    return f.client_for(user).patch(f"/api/bookings/{booking.pk}/input-values/", {"input_values": values}, format="json")


# --- charge calculation ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_set_one_formula_still_applies(egs_factory):
    eq = _apreo(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    assert _calc(client, eq, 4, 1, []).status_code == 200
    over = _calc(client, eq, 5, 1, [])
    assert over.status_code == 400
    assert over.data["error"] == _formula_error(4, 1, prefix="")


@pytest.mark.django_db
def test_set_two_a_above_its_own_four_times_b_is_rejected_and_named(egs_factory):
    eq = _apreo(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    resp = _calc(client, eq, 8, 2, [{"A": 9, "B": 2, "C": "No"}])

    assert resp.status_code == 400
    assert resp.data["error"] == _formula_error(8, 2)


@pytest.mark.django_db
def test_set_two_uses_its_own_b_not_set_one(egs_factory):
    eq = _apreo(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    # Set 1 has B = 5 (A up to 20) but set 2's B = 1 limits set 2's A to 4.
    small = _calc(client, eq, 1, 5, [{"A": 5, "B": 1}])
    assert small.status_code == 400
    assert small.data["error"] == _formula_error(4, 1)

    # Set 2's own larger B allows a larger A than set 1's B would.
    large = _calc(client, eq, 1, 1, [{"A": 12, "B": 3}])
    assert large.status_code == 200, large.data
    assert large.data["total_time_minutes"] == 90 + 270


@pytest.mark.django_db
def test_third_set_is_checked_on_its_own_values(egs_factory):
    eq = _apreo(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    resp = _calc(client, eq, 4, 1, [{"A": 8, "B": 2}, {"A": 9, "B": 2}])

    assert resp.status_code == 400
    assert resp.data["error"] == _formula_error(8, 2, prefix="Sample set 3: ")


@pytest.mark.django_db
def test_combined_b_max_and_minimum_of_one_still_apply_with_formula(egs_factory):
    eq = _apreo(egs_factory, b_help_text="1\n2\n1")
    client = egs_factory.client_for(egs_factory.student())

    combined = _calc(client, eq, 4, 1, [{"A": 8, "B": 2}])
    assert combined.status_code == 400
    assert combined.data["error"] == (
        f"Total {B_LABEL} across all sample sets (3) exceeds the maximum allowed (2) for this equipment."
    )
    zero = _calc(client, eq, 4, 1, [{"A": 0, "B": 1}])
    assert zero.status_code == 400
    assert zero.data["error"] == "Sample set 2: No. of Samples cannot be less than 1."
    assert _calc(client, eq, 4, 1, [{"A": 4, "B": 1}]).status_code == 200


# --- booking creation -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_rejects_set_two_over_its_formula(egs_factory, no_portal_lock):
    eq = _apreo(egs_factory)
    student = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(eq, egs_factory.future(), minutes=90)

    rejected = _book(egs_factory, student, eq, slot, {"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 5, "B": 1}]})

    assert rejected.status_code == 400
    assert rejected.data["error"] == _formula_error(4, 1)
    assert not Booking.objects.filter(user=student).exists()


@pytest.mark.django_db
def test_create_accepts_set_two_within_its_own_formula(egs_factory, no_portal_lock):
    eq = _apreo(egs_factory)
    ChargeProfile.objects.filter(equipment=eq).update(time_formula="45")
    student = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(eq, egs_factory.future(), minutes=90)

    resp = _book(egs_factory, student, eq, slot, {"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 8, "B": 2}]})

    assert resp.status_code == 201, resp.data
    assert Booking.objects.get(user=student).input_values[SAMPLE_SETS_KEY] == [{"A": 8, "B": 2}]


# --- edits ------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_rejects_set_two_over_its_formula_for_owner_and_oic(egs_factory):
    eq = _apreo(egs_factory, enable_charge_recalculation=True)
    owner = egs_factory.student()
    stored = {"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 4, "B": 1}]}
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values=stored)
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)

    for editor in (owner, oic, UserFactory(user_type=UserType.ADMIN)):
        resp = _patch(egs_factory, editor, booking, {"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 5, "B": 1}]})
        assert resp.status_code == 400, (editor.user_type, resp.data)
        assert resp.data["error"] == _formula_error(4, 1)
        # Lowering set 2's B below what its A needs is caught too.
        resp = _patch(egs_factory, editor, booking, {"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 8, "B": 1}]})
        assert resp.status_code == 400, (editor.user_type, resp.data)

    booking.refresh_from_db()
    assert booking.input_values == stored

    ok = _patch(egs_factory, owner, booking, {"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 8, "B": 2}]})
    assert ok.status_code == 200, ok.data
    booking.refresh_from_db()
    assert booking.input_values[SAMPLE_SETS_KEY] == [{"A": 8, "B": 2}]


# --- templates --------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_template_sets_are_held_to_their_own_formula(egs_factory):
    eq = _apreo(egs_factory)
    client = egs_factory.client_for(egs_factory.student())
    url = "/api/booking-templates/"

    bad = client.post(
        url,
        {"equipment": eq.pk, "name": "Too many", "input_values": {"A": "4", "B": "1", SAMPLE_SETS_KEY: [{"A": "9", "B": "2"}]}},
        format="json",
    )
    assert bad.status_code == 400
    assert bad.data["error"] == _formula_error(8, 2)

    ok = client.post(
        url,
        {"equipment": eq.pk, "name": "Fits", "input_values": {"A": "1", "B": "1", SAMPLE_SETS_KEY: [{"A": "12", "B": "3"}]}},
        format="json",
    )
    assert ok.status_code == 201, ok.data

    edit = client.patch(
        f"{url}{ok.data['id']}/",
        {"input_values": {"A": "1", "B": "1", SAMPLE_SETS_KEY: [{"A": "12", "B": "2"}]}},
        format="json",
    )
    assert edit.status_code == 400
    assert edit.data["error"] == _formula_error(8, 2)
    assert BookingInputTemplate.objects.get(pk=ok.data["id"]).input_values[SAMPLE_SETS_KEY] == [{"A": "12", "B": "3"}]


@pytest.mark.django_db
def test_template_set_one_formula_and_unchanged_legacy_sets(egs_factory):
    eq = _apreo(egs_factory)
    user = egs_factory.student()
    client = egs_factory.client_for(user)
    url = "/api/booking-templates/"

    bad = client.post(url, {"equipment": eq.pk, "name": "One", "input_values": {"A": "5", "B": "1"}}, format="json")
    assert bad.status_code == 400
    assert bad.data["error"] == _formula_error(4, 1, prefix="")

    legacy_values = {"A": "1", "B": "1", SAMPLE_SETS_KEY: [{"A": "9", "B": "1"}]}
    legacy = BookingInputTemplate.objects.create(user=user, equipment=eq, name="Legacy", input_values=legacy_values)
    kept = client.patch(
        f"{url}{legacy.pk}/", {"input_values": {**legacy_values, "A": "2"}}, format="json"
    )
    assert kept.status_code == 200, kept.data


# --- proforma ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_proforma_checks_every_sets_formula(egs_factory):
    eq = _apreo(egs_factory)
    client = egs_factory.client_for(egs_factory.student())
    url = "/api/proforma-invoice/calculate/"

    def post(values):
        return client.post(url, {"items": [{"equipment_id": eq.pk, "input_values": values}]}, format="json")

    bad = post({"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 9, "B": 2}]})
    assert bad.status_code == 400
    assert bad.data["error"] == f"{eq.code}: {_formula_error(8, 2)}"

    zero = post({"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 0, "B": 1}]})
    assert zero.status_code == 400
    assert zero.data["error"] == f"{eq.code}: Sample set 2: No. of Samples cannot be less than 1."

    ok = post({"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 12, "B": 3}]})
    assert ok.status_code == 200, ok.data


# --- helpers ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_legacy_plain_string_formula_is_honoured(egs_factory):
    eq = _apreo(egs_factory, a_options="B*4")
    student = egs_factory.student()

    assert _sample_set_groups_limit_error(eq, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 5, "B": 1}]}, student) == (
        _formula_error(4, 1)
    )
    assert _sample_set_groups_limit_error(eq, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 4, "B": 1}]}, student) is None


@pytest.mark.django_db
def test_external_users_skip_field_a_formula_as_in_set_one(egs_factory):
    eq = _apreo(egs_factory)
    external = UserFactory(user_type=UserType.EXTERNAL, department=egs_factory.department)

    assert _sample_set_groups_limit_error(eq, {"A": 9, "B": 1, SAMPLE_SETS_KEY: [{"A": 9, "B": 1}]}, external) is None
