"""Field A / B maximums apply to all sample sets of a booking combined, and only the equipment's OIC or a
main administrator may add or remove sample sets once the booking exists."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.calculators import SAMPLE_SETS_KEY
from iic_booking.equipment.models import (
    Booking,
    BookingInputTemplate,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
    EquipmentTemporaryOIC,
)
from iic_booking.equipment.sample_set_limits import combined_max_error
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.tests.factories import UserFactory


def _equipment(egs_factory, **kwargs):
    """A = No. of Samples (options max 4); B = No. of Slots (help_text max 2, like FE-SEM APREO)."""
    eq = egs_factory.equipment(time_formula="30", **kwargs)
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 4},
        editing_required=True,
    )
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="B",
        field_label="No. of Slots",
        field_type=DynamicInputFieldType.NUMERIC,
        help_text="1\n2\n1",
        editing_required=True,
    )
    return eq


def _patch(egs_factory, user, booking, values):
    return egs_factory.client_for(user).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": values}, format="json"
    )


def _oic(egs_factory, eq):
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic


def _student_with_wallet(f, balance="10000.00"):
    student = f.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=f.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student,
        faculty=faculty,
        wallet=wallet,
        status=WalletJoinRequestStatus.APPROVED,
        responded_at=timezone.now() - timedelta(days=30),
    )
    SubWalletRepository.get_or_create(wallet, f.department).credit(Decimal(balance), description="Recharge")
    return student


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _book(f, user, eq, slot, input_values):
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": input_values,
        "waitlist_on_failure": False,
    }
    return f.client_for(user).post(f"/api/equipments/{eq.pk}/book/", body, format="json")


# --- booking creation -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_rejected_when_combined_b_exceeds_max(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(eq, egs_factory.future())

    resp = _book(egs_factory, student, eq, slot, {"A": 1, "B": 2, SAMPLE_SETS_KEY: [{"A": 1, "B": 2}]})

    assert resp.status_code == 400
    assert resp.data["error"] == (
        "Total No. of Slots across all sample sets (4) exceeds the maximum allowed (2) for this equipment."
    )
    assert not Booking.objects.filter(user=student).exists()


@pytest.mark.django_db
def test_create_allowed_at_exactly_the_max(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(eq, egs_factory.future())

    resp = _book(egs_factory, student, eq, slot, {"A": 2, "B": 1, SAMPLE_SETS_KEY: [{"A": 2, "B": 1}]})

    assert resp.status_code == 201, resp.data
    booking = Booking.objects.get(user=student)
    assert booking.input_values[SAMPLE_SETS_KEY] == [{"A": 2, "B": 1}]
    assert booking.total_time_minutes == 60


@pytest.mark.django_db
def test_field_a_is_checked_too(egs_factory):
    eq = _equipment(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    resp = client.get(
        f"/api/equipments/{eq.pk}/calculate/",
        {"A": 3, "B": 1, "sample_sets": json.dumps([{"A": 2, "B": 1}])},
    )

    assert resp.status_code == 400
    assert resp.data["error"] == (
        "Total No. of Samples across all sample sets (5) exceeds the maximum allowed (4) for this equipment."
    )
    ok = client.get(
        f"/api/equipments/{eq.pk}/calculate/",
        {"A": 2, "B": 1, "sample_sets": json.dumps([{"A": 2, "B": 1}])},
    )
    assert ok.status_code == 200, ok.data


@pytest.mark.django_db
def test_per_set_limits_still_apply(egs_factory):
    eq = _equipment(egs_factory)
    client = egs_factory.client_for(egs_factory.student())

    resp = client.get(f"/api/equipments/{eq.pk}/calculate/", {"A": 1, "B": 1, "sample_sets": json.dumps([{"B": 3}])})

    assert resp.status_code == 400
    assert resp.data["error"] == "Sample set 2: No. of Slots cannot be greater than 2."


@pytest.mark.django_db
def test_formula_max_and_unconfigured_max_are_not_summed(egs_factory):
    eq = egs_factory.equipment(time_formula="30")
    DynamicInputField.objects.create(
        equipment=eq, field_key="A", field_label="Samples", field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max_formula": "B*4"},
    )
    DynamicInputField.objects.create(
        equipment=eq, field_key="B", field_label="Hours", field_type=DynamicInputFieldType.NUMERIC,
    )

    values = {"A": 4, "B": 60, SAMPLE_SETS_KEY: [{"A": 4, "B": 60}]}
    assert combined_max_error(eq, values) is None


# --- FE-SEM APREO field shape -----------------------------------------------------------------------


def _apreo(egs_factory, b_help_text="", **kwargs):
    """Production APREO rows (typed per user type): A max_formula B*4, B options [] with no help text.
    The equipment form only offers help-text line 2 for a NUMERIC maximum, so "1\\n2\\n1" is how B max 2 is set."""
    eq = egs_factory.equipment(time_formula="B*90", **kwargs)
    DynamicInputField.objects.create(
        equipment=eq, user_type=UserType.STUDENT, field_key="A", field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC, options={"min": 1, "max_formula": "B*4"},
        default_value="1", is_required=True, editing_required=True,
    )
    DynamicInputField.objects.create(
        equipment=eq, user_type=UserType.STUDENT, field_key="B",
        field_label="Number of Slots ( Slot Duration: 1.5 Hours )", field_type=DynamicInputFieldType.NUMERIC,
        options=[], help_text=b_help_text, default_value="1", is_required=True,
    )
    return eq


APREO_B_TOTAL_ERROR = (
    "Total Number of Slots ( Slot Duration: 1.5 Hours ) across all sample sets (4) exceeds the maximum allowed (2) "
    "for this equipment."
)


@pytest.mark.django_db
def test_apreo_as_configured_has_no_b_maximum_to_sum(egs_factory):
    eq = _apreo(egs_factory)
    student = egs_factory.student()

    assert combined_max_error(eq, {"A": 4, "B": 2, SAMPLE_SETS_KEY: [{"A": 4, "B": 2}]}, booking_user=student) is None


@pytest.mark.django_db
def test_apreo_b_max_2_combined_across_sets_on_create(egs_factory, no_portal_lock):
    eq = _apreo(egs_factory, b_help_text="1\n2\n1")
    student = _student_with_wallet(egs_factory)
    client = egs_factory.client_for(student)

    over = client.get(
        f"/api/equipments/{eq.pk}/calculate/", {"A": 2, "B": 2, "sample_sets": json.dumps([{"A": 2, "B": 2}])}
    )
    assert over.status_code == 400
    assert over.data["error"] == APREO_B_TOTAL_ERROR

    slot = egs_factory.slot(eq, egs_factory.future())
    rejected = _book(egs_factory, student, eq, slot, {"A": 2, "B": 2, SAMPLE_SETS_KEY: [{"A": 2, "B": 2}]})
    assert rejected.status_code == 400
    assert rejected.data["error"] == APREO_B_TOTAL_ERROR
    assert not Booking.objects.filter(user=student).exists()

    ok = client.get(
        f"/api/equipments/{eq.pk}/calculate/", {"A": 4, "B": 1, "sample_sets": json.dumps([{"A": 4, "B": 1}])}
    )
    assert ok.status_code == 200, ok.data
    assert ok.data["total_time_minutes"] == 180


@pytest.mark.django_db
def test_apreo_field_a_formula_still_checked_per_set(egs_factory):
    eq = _apreo(egs_factory, b_help_text="1\n2\n1")
    client = egs_factory.client_for(egs_factory.student())

    resp = client.get(
        f"/api/equipments/{eq.pk}/calculate/", {"A": 4, "B": 1, "sample_sets": json.dumps([{"A": 5, "B": 1}])}
    )

    assert resp.status_code == 400
    assert resp.data["error"].startswith("Sample set 2: No. of Samples cannot be greater than 4")


@pytest.mark.django_db
def test_apreo_b_max_2_combined_across_sets_on_edit(egs_factory):
    eq = _apreo(egs_factory, b_help_text="1\n2\n1", enable_charge_recalculation=True)
    owner = egs_factory.student()
    one_plus_one = {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]}
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values=one_plus_one)

    raised = _patch(egs_factory, owner, booking, {"A": 1, "B": 2, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]})
    assert raised.status_code == 400, raised.data
    assert "across all sample sets (3) exceeds the maximum allowed (2)" in raised.data["error"]

    added = _patch(egs_factory, _oic(egs_factory, eq), booking, {**one_plus_one, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}] * 2})
    assert added.status_code == 400, added.data
    assert "across all sample sets (3) exceeds the maximum allowed (2)" in added.data["error"]

    within = _patch(egs_factory, owner, booking, {"A": 4, "B": 1, SAMPLE_SETS_KEY: [{"A": 3, "B": 1}]})
    assert within.status_code == 200, within.data
    booking.refresh_from_db()
    assert booking.input_values[SAMPLE_SETS_KEY] == [{"A": 3, "B": 1}]


# --- editing booked parameters ----------------------------------------------------------------------


@pytest.mark.django_db
def test_edit_pushing_combined_total_over_max_is_rejected(egs_factory):
    eq = _equipment(egs_factory, enable_charge_recalculation=True)
    owner = egs_factory.student()
    booking = egs_factory.booking(
        owner, eq, egs_factory.future(), input_values={"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]}
    )

    for editor in (owner, _oic(egs_factory, eq), UserFactory(user_type=UserType.ADMIN)):
        resp = _patch(egs_factory, editor, booking, {"A": 1, "B": 2, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]})
        assert resp.status_code == 400, (editor.user_type, resp.data)
        assert "Total No. of Slots across all sample sets (3)" in resp.data["error"]
        # Editing set 1 without resending the sets is checked against the stored sets too.
        resp = _patch(egs_factory, editor, booking, {"A": 1, "B": 2})
        assert resp.status_code == 400, (editor.user_type, resp.data)

    booking.refresh_from_db()
    assert booking.input_values == {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]}
    assert booking.total_charge == Decimal("10.00")


@pytest.mark.django_db
def test_user_may_edit_values_inside_existing_sets_within_the_max(egs_factory):
    eq = _equipment(egs_factory, enable_charge_recalculation=True)
    owner = egs_factory.student()
    booking = egs_factory.booking(
        owner, eq, egs_factory.future(), input_values={"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]}
    )

    resp = _patch(egs_factory, owner, booking, {"A": 2, "B": 1, SAMPLE_SETS_KEY: [{"A": 2, "B": 1}]})

    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values[SAMPLE_SETS_KEY] == [{"A": 2, "B": 1}]


@pytest.mark.django_db
def test_legacy_booking_over_the_max_may_keep_or_lower_its_total(egs_factory):
    eq = _equipment(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(
        owner, eq, egs_factory.future(), input_values={"A": 1, "B": 2, SAMPLE_SETS_KEY: [{"A": 1, "B": 2}]}
    )

    resp = _patch(egs_factory, owner, booking, {"A": 2, "B": 2, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]})

    assert resp.status_code == 200, resp.data


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["owner", "operator", "dept_admin", "other_oic"])
def test_only_oic_or_admin_can_add_or_remove_sets_after_booking(egs_factory, role):
    from iic_booking.users.rbac import ensure_default_dept_admin_permission_grants

    eq = _equipment(egs_factory)
    owner = egs_factory.student()
    one_set = {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]}
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values=one_set)
    if role == "owner":
        editor = owner
    elif role == "operator":
        editor = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)
    elif role == "dept_admin":
        editor = UserFactory(user_type=UserType.DEPT_ADMIN, department=egs_factory.department, admin_approved=True)
        ensure_default_dept_admin_permission_grants(editor)
    else:
        editor = _oic(egs_factory, egs_factory.equipment())

    added = _patch(egs_factory, editor, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1}, {"A": 1}]})
    assert added.status_code == 403, added.data
    assert added.data["code"] == "sample_sets_locked"
    removed = _patch(egs_factory, editor, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: []})
    assert removed.status_code == 403, removed.data

    booking.refresh_from_db()
    assert booking.input_values == one_set


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["oic", "temporary_oic", "admin", "superuser"])
def test_oic_and_admin_can_add_and_remove_sets_within_the_max(egs_factory, role):
    eq = _equipment(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 1, "B": 1})
    if role == "oic":
        editor = _oic(egs_factory, eq)
    elif role == "temporary_oic":
        editor = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
        EquipmentTemporaryOIC.objects.create(
            equipment=eq, primary_oic=_oic(egs_factory, eq), temporary_oic=editor,
            resume_at=timezone.now() + timedelta(days=2),
        )
    elif role == "admin":
        editor = UserFactory(user_type=UserType.ADMIN)
    else:
        editor = UserFactory(user_type=UserType.ADMIN, is_superuser=True)

    over = _patch(egs_factory, editor, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 2}]})
    assert over.status_code == 400, over.data
    assert "exceeds the maximum allowed (2)" in over.data["error"]

    added = _patch(egs_factory, editor, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: [{"A": 1, "B": 1}]})
    assert added.status_code == 200, added.data
    booking.refresh_from_db()
    assert booking.input_values[SAMPLE_SETS_KEY] == [{"A": 1, "B": 1}]

    removed = _patch(egs_factory, editor, booking, {"A": 1, "B": 1, SAMPLE_SETS_KEY: []})
    assert removed.status_code == 200, removed.data
    booking.refresh_from_db()
    assert SAMPLE_SETS_KEY not in booking.input_values


@pytest.mark.django_db
def test_booking_payload_tells_the_viewer_whether_they_can_change_sets(egs_factory):
    eq = _equipment(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 1, "B": 1})
    oic = _oic(egs_factory, eq)

    def flag(user):
        resp = egs_factory.client_for(user).get("/api/bookings/", {"booking_id": booking.pk, "limit": 1})
        assert resp.status_code == 200, resp.data
        return resp.data["bookings"][0]["viewer_can_change_sample_sets"]

    assert flag(owner) is False
    assert flag(oic) is True
    assert flag(UserFactory(user_type=UserType.ADMIN)) is True

    edited = _patch(egs_factory, oic, booking, {"A": 2, "B": 1})
    assert edited.status_code == 200, edited.data
    assert edited.data["booking"]["viewer_can_change_sample_sets"] is True


# --- booking templates ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_template_with_sets_over_the_max_is_rejected(egs_factory):
    eq = _equipment(egs_factory)
    user = egs_factory.student()
    client = egs_factory.client_for(user)
    url = "/api/booking-templates/"

    bad = client.post(
        url,
        {"equipment": eq.pk, "name": "Too many", "input_values": {"B": "2", SAMPLE_SETS_KEY: [{"B": "1"}]}},
        format="json",
    )
    assert bad.status_code == 400
    assert "Total No. of Slots across all sample sets (3)" in bad.data["error"]

    ok = client.post(
        url,
        {"equipment": eq.pk, "name": "Fits", "input_values": {"B": "1", SAMPLE_SETS_KEY: [{"B": "1"}]}},
        format="json",
    )
    assert ok.status_code == 201, ok.data
    edit = client.patch(
        f"{url}{ok.data['id']}/", {"input_values": {"B": "2", SAMPLE_SETS_KEY: [{"B": "2"}]}}, format="json"
    )
    assert edit.status_code == 400
    assert BookingInputTemplate.objects.get(pk=ok.data["id"]).input_values["B"] == "1"
