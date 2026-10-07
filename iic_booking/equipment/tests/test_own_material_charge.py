"""Charge for IIC material used on own-material 3D print / laser bookings: pricing, wallet debit through the
existing Deduct Money path, insufficient balance, permissions, eligibility, audit event, reversal and
recalculation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.fabrication import inject_print_parts, strip_fabrication_keys
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    BookingMaterialCharge,
    BookingStatus,
    EquipmentManager,
    EquipmentTemporaryOIC,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import SubWalletTransaction
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import acrylic_3mm, funded_student, laser_equipment, print_equipment, print_material, print_part


@pytest.fixture(autouse=True)
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _oic_for(egs_factory, eq):
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return oic


def _laser(egs_factory, *, balance="10000.00", own_material=True, status=BookingStatus.BOOKED):
    eq = laser_equipment(egs_factory, own_charge="500.00")
    sheet = acrylic_3mm(eq, code="MS-1", rate="1000.00", name="MS-1")
    owner, wallet = funded_student(egs_factory, balance=balance)
    booking = egs_factory.booking(
        owner, eq, egs_factory.future(), total_charge="500.00", own_material=own_material
    )
    if status != BookingStatus.BOOKED:
        booking.status = status
        booking.save(update_fields=["status"])
    return eq, sheet, owner, wallet, booking, _oic_for(egs_factory, eq)


def _url(booking, suffix=""):
    return f"/api/bookings/{booking.pk}/material-charges/{suffix}"


def _charge(egs_factory, user, booking, **body):
    return egs_factory.client_for(user).post(_url(booking), body, format="json")


def _preview(egs_factory, user, booking, **body):
    return egs_factory.client_for(user).post(_url(booking, "preview/"), body, format="json")


REASON = "User's sheet was insufficient; 1 IIC MS sheet used"


# --------------------------------------------------------------------------- pricing


@pytest.mark.django_db
def test_laser_price_per_sheet_and_part_sheet_for_internal_user(egs_factory):
    _eq, sheet, _owner, _wallet, booking, oic = _laser(egs_factory)

    full = _preview(egs_factory, oic, booking, material_id=sheet.pk, quantity="1")
    assert full.status_code == 200, full.data
    assert (full.data["base_amount"], full.data["gst_amount"], full.data["amount"]) == ("1000.00", "0.00", "1000.00")
    assert full.data["unit"] == "sheet"
    assert full.data["line"] == "IIC material used: MS-1 × 1 sheet"
    assert full.data["collection"]["mode"] == "deduct"

    quarter = _preview(egs_factory, oic, booking, material_id=sheet.pk, quantity="0.25")
    assert quarter.status_code == 200, quarter.data
    assert quarter.data["amount"] == "250.00"

    too_precise = _preview(egs_factory, oic, booking, material_id=sheet.pk, quantity="0.125")
    assert too_precise.status_code == 400


@pytest.mark.django_db
def test_external_user_pays_gst_on_top(egs_factory):
    _eq, sheet, _owner, _wallet, booking, oic = _laser(egs_factory)
    booking.user_type_snapshot = UserType.EXTERNAL
    booking.save(update_fields=["user_type_snapshot"])

    resp = _preview(egs_factory, oic, booking, material_id=sheet.pk, quantity="1")
    assert resp.status_code == 200, resp.data
    assert (resp.data["base_amount"], resp.data["gst_percent"], resp.data["gst_amount"], resp.data["amount"]) == (
        "1000.00", "18", "180.00", "1180.00"
    )


@pytest.mark.django_db
def test_print_price_per_whole_gram_with_and_without_gst(egs_factory):
    eq = print_equipment(egs_factory, own_charge="50.00")
    pla = print_material(eq, price="1.4400")
    owner, _wallet = funded_student(egs_factory)
    booking = egs_factory.booking(owner, eq, egs_factory.future(), total_charge="50.00", own_material=True)
    oic = _oic_for(egs_factory, eq)

    resp = _preview(egs_factory, oic, booking, material_id=pla.pk, quantity="40.2")
    assert resp.status_code == 200, resp.data
    # ceil(40.2) = 41 g × 1.44 = 59.04 -> ₹59
    assert (resp.data["quantity"], resp.data["unit"], resp.data["amount"]) == ("41", "g", "59.00")

    booking.user_type_snapshot = UserType.EXTERNAL
    booking.save(update_fields=["user_type_snapshot"])
    ext = _preview(egs_factory, oic, booking, material_id=pla.pk, quantity="40.2")
    # 59 + GST 18% (10.62 -> 11) = 70
    assert (ext.data["base_amount"], ext.data["gst_amount"], ext.data["amount"]) == ("59.00", "11.00", "70.00")


@pytest.mark.django_db
def test_material_must_be_supported_and_enabled_on_the_equipment(egs_factory):
    _eq, sheet, _owner, _wallet, booking, oic = _laser(egs_factory)
    other_eq = laser_equipment(egs_factory, own_charge="100.00")
    foreign = acrylic_3mm(other_eq, code="FOREIGN")

    assert _preview(egs_factory, oic, booking, material_id=foreign.pk, quantity="1").status_code == 400
    sheet.is_active = False
    sheet.save(update_fields=["is_active"])
    assert _preview(egs_factory, oic, booking, material_id=sheet.pk, quantity="1").status_code == 400


# --------------------------------------------------------------------------- posting the charge


@pytest.mark.django_db
def test_charge_is_deducted_from_the_wallet_and_audited(egs_factory):
    _eq, sheet, _owner, wallet, booking, oic = _laser(egs_factory)

    resp = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON)
    assert resp.status_code == 201, resp.data
    assert resp.data["summary"]["deducted_amount"] == "1000.00"

    wallet.refresh_from_db()
    booking.refresh_from_db()
    assert wallet.balance == Decimal("9000.00")
    assert booking.total_charge == Decimal("1500.00")
    assert booking.charge_recalculation_pending_amount is None
    line = booking.charge_breakdown[-1]
    assert line["description"] == "IIC material used: MS-1 × 1 sheet"
    assert Decimal(str(line["amount"])) == Decimal("1000")

    charge = BookingMaterialCharge.objects.get(booking=booking)
    assert charge.created_by == oic
    assert charge.reason == REASON
    txn = SubWalletTransaction.objects.get(pk=charge.wallet_transaction_id)
    assert txn.transaction_type == SubWalletTransaction.TransactionType.DEBIT
    assert txn.amount == Decimal("1000.00")
    assert "IIC material used (MS-1 × 1 sheet)" in txn.description

    event = BookingEvent.objects.get(booking=booking, event_type=BookingEventType.CHARGE_RECALCULATED)
    assert event.created_by == oic
    meta = event.metadata["material_charge"]
    assert (meta["material_code"], meta["quantity"], meta["unit"], meta["unit_price"]) == ("MS-1", "1", "sheet", "1000.00")
    assert (meta["gst_amount"], meta["amount"], meta["reason"]) == ("0.00", "1000.00", REASON)
    assert event.metadata["amount_debited"] == "1000.00"
    assert "deducted from the wallet" in event.comment


@pytest.mark.django_db
def test_insufficient_balance_leaves_the_amount_to_pay(egs_factory):
    _eq, sheet, _owner, wallet, booking, oic = _laser(egs_factory, balance="100.00")

    preview = _preview(egs_factory, oic, booking, material_id=sheet.pk, quantity="1")
    assert preview.data["collection"]["mode"] == "pay_now"

    resp = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON)
    assert resp.status_code == 201, resp.data
    assert resp.data["summary"]["extra_amount"] == "1000.00"
    wallet.refresh_from_db()
    booking.refresh_from_db()
    assert wallet.balance == Decimal("100.00")
    assert booking.total_charge == Decimal("1500.00")
    assert booking.charge_recalculation_pending_amount == Decimal("1000.00")
    charge = BookingMaterialCharge.objects.get(booking=booking)
    assert charge.wallet_transaction_id is None
    event = BookingEvent.objects.get(booking=booking, event_type=BookingEventType.CHARGE_RECALCULATED)
    assert "click Pay Now" in event.comment
    assert event.metadata["extra_amount"] == "1000.00"

    # After a recharge, Deduct Money collects it as for any other extra amount.
    wallet.credit(Decimal("2000.00"), description="Recharge")
    pay = egs_factory.client_for(oic).post(
        f"/api/bookings/{booking.pk}/process-charge-recalculation-pay-now/", {}, format="json"
    )
    assert pay.status_code == 200, pay.data
    wallet.refresh_from_db()
    booking.refresh_from_db()
    assert wallet.balance == Decimal("1100.00")
    assert booking.charge_recalculation_pending_amount is None


@pytest.mark.django_db
def test_multiple_charges_each_get_a_line(egs_factory):
    _eq, sheet, _owner, wallet, booking, oic = _laser(egs_factory)
    assert _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON).status_code == 201
    assert _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="0.5", reason="Second shortfall").status_code == 201
    booking.refresh_from_db()
    wallet.refresh_from_db()
    assert booking.total_charge == Decimal("2000.00")
    assert wallet.balance == Decimal("8500.00")
    lines = [line for line in booking.charge_breakdown if line.get("material_charge_id")]
    assert [line["description"] for line in lines] == [
        "IIC material used: MS-1 × 1 sheet",
        "IIC material used: MS-1 × 0.5 sheet",
    ]
    assert BookingMaterialCharge.objects.filter(booking=booking).count() == 2


@pytest.mark.django_db
def test_reason_is_mandatory(egs_factory):
    _eq, sheet, _owner, _wallet, booking, oic = _laser(egs_factory)
    resp = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason="  ")
    assert resp.status_code == 400
    assert not BookingMaterialCharge.objects.exists()


# --------------------------------------------------------------------------- permissions and eligibility


@pytest.mark.django_db
def test_oic_substitute_and_admin_may_charge_others_may_not(egs_factory):
    eq, sheet, owner, _wallet, booking, oic = _laser(egs_factory)
    substitute = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentTemporaryOIC.objects.create(
        equipment=eq, primary_oic=oic, temporary_oic=substitute, resume_at=timezone.now() + timedelta(days=3)
    )
    admin = UserFactory(user_type=UserType.ADMIN, department=egs_factory.department, admin_approved=True)
    other_oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)

    for denied in (owner, other_oic, operator):
        assert egs_factory.client_for(denied).get(_url(booking)).status_code == 403
        assert _charge(egs_factory, denied, booking, material_id=sheet.pk, quantity="1", reason=REASON).status_code == 403
    assert APIClient().get(_url(booking)).status_code in (401, 403)

    for allowed in (oic, substitute, admin):
        overview = egs_factory.client_for(allowed).get(_url(booking))
        assert overview.status_code == 200, overview.data
        assert overview.data["eligible"] is True
        assert [m["code"] for m in overview.data["materials"]] == ["MS-1"]
        assert _charge(egs_factory, allowed, booking, material_id=sheet.pk, quantity="0.1", reason=REASON).status_code == 201
    assert BookingMaterialCharge.objects.filter(booking=booking).count() == 3


@pytest.mark.django_db
def test_only_the_main_admin_may_override_the_amount(egs_factory):
    _eq, sheet, _owner, wallet, booking, oic = _laser(egs_factory)
    admin = UserFactory(user_type=UserType.ADMIN, department=egs_factory.department, admin_approved=True)

    denied = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON, override_amount="700")
    assert denied.status_code == 403
    resp = _charge(egs_factory, admin, booking, material_id=sheet.pk, quantity="1", reason=REASON, override_amount="700")
    assert resp.status_code == 201, resp.data
    charge = BookingMaterialCharge.objects.get(booking=booking)
    assert (charge.computed_amount, charge.amount, charge.amount_overridden) == (
        Decimal("1000.00"), Decimal("700.00"), True
    )
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("9300.00")


@pytest.mark.django_db
def test_only_own_material_bookings_are_eligible(egs_factory):
    _eq, sheet, _owner, _wallet, booking, oic = _laser(egs_factory, own_material=False)
    overview = egs_factory.client_for(oic).get(_url(booking))
    assert overview.status_code == 200
    assert overview.data["eligible"] is False
    assert "did not bring their own material" in overview.data["ineligible_reason"]
    resp = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON)
    assert resp.status_code == 400
    assert resp.data["code"] == "NOT_ELIGIBLE"


@pytest.mark.django_db
def test_completed_bookings_allowed_cancelled_and_refunded_are_not(egs_factory):
    _eq, sheet, _owner, wallet, booking, oic = _laser(egs_factory, status=BookingStatus.COMPLETED)
    assert _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON).status_code == 201

    for blocked, word in ((BookingStatus.CANCELLED, "cancelled"), (BookingStatus.REFUNDED, "refunded")):
        booking.status = blocked
        booking.save(update_fields=["status"])
        resp = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON)
        assert resp.status_code == 400
        assert word in resp.data["error"]
        assert word in egs_factory.client_for(oic).get(_url(booking)).data["ineligible_reason"]
    assert BookingMaterialCharge.objects.filter(booking=booking).count() == 1


# --------------------------------------------------------------------------- reversal


@pytest.mark.django_db
def test_reversing_a_paid_charge_becomes_a_refund_the_oic_confirms(egs_factory):
    _eq, sheet, owner, wallet, booking, oic = _laser(egs_factory)
    created = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON)
    charge_id = created.data["charge"]["id"]

    url = _url(booking, f"{charge_id}/reverse/")
    assert egs_factory.client_for(owner).post(url, {"reason": "x"}, format="json").status_code == 403
    assert egs_factory.client_for(oic).post(url, {"reason": ""}, format="json").status_code == 400

    resp = egs_factory.client_for(oic).post(url, {"reason": "Charged on the wrong booking"}, format="json")
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.total_charge == Decimal("500.00")
    assert booking.charge_recalculation_pending_amount == Decimal("-1000.00")
    assert not [line for line in booking.charge_breakdown if line.get("material_charge_id")]
    charge = BookingMaterialCharge.objects.get(pk=charge_id)
    assert charge.reversed_by == oic and charge.reversal_reason == "Charged on the wrong booking"
    event = BookingEvent.objects.filter(booking=booking).order_by("-event_id").first()
    assert event.metadata["material_charge_reversed"]["id"] == charge_id
    assert event.metadata["refund_status"] == "awaiting_oic_confirmation"

    assert egs_factory.client_for(oic).post(url, {"reason": "again"}, format="json").status_code == 400

    confirm = egs_factory.client_for(oic).post(
        f"/api/bookings/{booking.pk}/process-charge-recalculation-refund/", {}, format="json"
    )
    assert confirm.status_code == 200, confirm.data
    wallet.refresh_from_db()
    assert wallet.balance == Decimal("10000.00")


@pytest.mark.django_db
def test_reversing_an_unpaid_charge_removes_the_amount_to_pay(egs_factory):
    _eq, sheet, _owner, wallet, booking, oic = _laser(egs_factory, balance="100.00")
    created = _charge(egs_factory, oic, booking, material_id=sheet.pk, quantity="1", reason=REASON)
    resp = egs_factory.client_for(oic).post(
        _url(booking, f"{created.data['charge']['id']}/reverse/"), {"reason": "Not used after all"}, format="json"
    )
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    wallet.refresh_from_db()
    assert booking.total_charge == Decimal("500.00")
    assert booking.charge_recalculation_pending_amount is None
    assert wallet.balance == Decimal("100.00")


# --------------------------------------------------------------------------- recalculation keeps the charge


@pytest.mark.django_db
def test_partial_cancel_of_print_files_does_not_refund_the_material_charge(egs_factory):
    from iic_booking.equipment.booking_cancellation import compute_partial_cancel_print_items

    eq = print_equipment(egs_factory, hourly_rate="60.00", own_charge="50.00")
    owner = egs_factory.student()
    pla = print_material(eq)
    # Files: ₹50 own-material fixed charge + 90 min at ₹60/h = ₹140, plus a ₹59 IIC material charge.
    booking = egs_factory.booking(owner, eq, egs_factory.future(), slot_count=3, total_charge="199.00", own_material=True)
    print_part(eq, owner, pla, weight="10", minutes=30, quantity=2, name="keep", booking=booking)
    drop = print_part(eq, owner, pla, weight="10", minutes=30, quantity=1, name="drop", booking=booking, sequence=1)
    BookingMaterialCharge.objects.create(
        booking=booking, profile_type=eq.profile_type, print_material=pla, material_code=pla.code,
        material_name=pla.name, quantity=Decimal("41"), unit="g", unit_price=pla.price_per_gram,
        base_amount=Decimal("59.00"), computed_amount=Decimal("59.00"), amount=Decimal("59.00"), reason=REASON,
    )

    plan = compute_partial_cancel_print_items(booking, [str(drop.id)])
    # Remaining files: ₹50 + 60 min = ₹110; the material charge stays.
    assert plan["refund_amount"] == Decimal("30.00")
    assert plan["new_charge"] == Decimal("169.00")
    assert any(line.get("material_charge_id") for line in plan["new_breakdown"])


@pytest.mark.django_db
def test_print_actuals_recalculation_keeps_the_material_charge(egs_factory):
    eq = print_equipment(egs_factory, hourly_rate="60.00", own_charge="50.00")
    pla = print_material(eq, price="1.4400")
    owner, wallet = funded_student(egs_factory)
    part = print_part(eq, owner, pla, weight="10.2", minutes=30)
    # Own material: fixed ₹50 + 30 min at ₹60/h = ₹80.
    booking = egs_factory.booking(
        owner,
        eq,
        egs_factory.future(),
        input_values=strip_fabrication_keys(inject_print_parts({}, [part])),
        total_charge="80.00",
        print_analysis=part,
        own_material=True,
    )
    part.booking = booking
    part.save(update_fields=["booking"])
    oic = _oic_for(egs_factory, eq)

    assert _charge(egs_factory, oic, booking, material_id=pla.pk, quantity="41", reason=REASON).status_code == 201
    booking.refresh_from_db()
    assert booking.total_charge == Decimal("139.00")

    resp = egs_factory.client_for(oic).patch(
        f"/api/bookings/{booking.pk}/print-actuals/", {"actual_weight_grams": 40, "actual_time_minutes": 60}, format="json"
    )
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    # ₹50 fixed + 60 min = ₹110, plus the ₹59 IIC material charge.
    assert booking.total_charge == Decimal("169.00")
    assert booking.charge_recalculation_pending_amount == Decimal("30.00")
    assert any(line.get("material_charge_id") for line in booking.charge_breakdown)
