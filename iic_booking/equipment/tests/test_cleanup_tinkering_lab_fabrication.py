"""cleanup_tinkering_lab_fabrication: cancel bookings through the standard path, finish the CNC laser switch, drop
the old CNC inputs, and replace the lab printers' own materials (and the bookings that refer to them) with the
master list. Deletes only after a backup; wallet rows never change."""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.files.storage import default_storage
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from iic_booking.equipment.fabrication_material_support import supported_materials
from iic_booking.equipment.management.commands.apply_tinkering_lab_fab_materials import DEFAULT_DEPARTMENT
from iic_booking.equipment.management.commands.cleanup_tinkering_lab_fabrication import (
    ARCHIVE_PREFIX,
    CNC_REASON,
    money_snapshot,
)
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    ChargeProfile,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentCategory,
    EquipmentProfileType,
    PrintAnalysis,
    PrintMaterial,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import SubWalletTransaction
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import acrylic_3mm, funded_student, laser_equipment, print_equipment, print_material, print_part

pytestmark = pytest.mark.django_db

HOUR = EquipmentProfileType.HOUR
SAMPLE = EquipmentProfileType.SAMPLE
LASER = EquipmentProfileType.LASER_CUT_2D


def _run(*args) -> str:
    out = StringIO()
    call_command("cleanup_tinkering_lab_fabrication", *args, stdout=out)
    return out.getvalue()


def _ids(qs):
    return sorted(qs.values_list("pk", flat=True))


@pytest.fixture
def lab(egs_factory):
    admin = UserFactory(user_type=UserType.ADMIN)
    type(admin).objects.filter(pk=admin.pk).update(is_active=True)
    dept = Department.objects.create(
        name=DEFAULT_DEPARTMENT, code="TLX", department_type=DepartmentType.INTERNAL,
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )
    egs_factory.department = dept
    cnc_cat = EquipmentCategory.objects.create(name="CNC Machine Tools")
    printer_cat = EquipmentCategory.objects.create(name="3D Printers")

    def cnc(profile=HOUR, time_formula="", with_b=True):
        eq = egs_factory.equipment(
            with_profile=False, profile_type=profile, category=cnc_cat, internal_department=dept,
            slot_duration_minutes=480,
        )
        for pricing in ("standard", "discounted"):
            ChargeProfile.objects.create(
                equipment=eq, user_type=UserType.STUDENT, pricing_profile=pricing, profile_type=profile,
                time_formula=time_formula, primary_unit_charge=Decimal("0.00"),
            )
        DynamicInputField.objects.create(
            equipment=eq, user_type=UserType.STUDENT, field_key="A", field_label="No. of Parts",
            field_type=DynamicInputFieldType.NUMERIC, is_required=True,
        )
        if with_b:
            DynamicInputField.objects.create(
                equipment=eq, user_type=UserType.STUDENT, field_key="B",
                field_label="Number of Slots ( Slot Duration: 8 Hours )",
                field_type=DynamicInputFieldType.NUMERIC, is_required=True,
            )
        return eq

    def printer():
        return print_equipment(egs_factory, category=printer_cat, internal_department=dept)

    sheet_owner = laser_equipment(egs_factory, internal_department=Department.objects.create(name="IIC", code="IICX"))
    print_owner = print_equipment(egs_factory, internal_department=sheet_owner.internal_department)
    return {"dept": dept, "cnc": cnc, "printer": printer, "sheet_owner": sheet_owner, "print_owner": print_owner}


def _past_booking(egs_factory, owner, equipment, status=BookingStatus.COMPLETED, **fields):
    start = timezone.now() - timedelta(days=10)
    booking = egs_factory.booking(owner, equipment, start, **fields)
    Booking.objects.filter(pk=booking.pk).update(status=status)
    booking.refresh_from_db()
    return booking


def _apply_all(tmp_path):
    backup = tmp_path / "backup"
    _run("--stage", "config", "--apply", "--backup-dir", str(backup))
    _run("--stage", "backup", "--backup-dir", str(backup), "--run-id", "t1")
    return backup, _run("--stage", "delete", "--apply", "--backup-dir", str(backup))


def test_cnc_booking_cancelled_then_switched_inputs_removed_and_formulas_cleared(lab, egs_factory, tmp_path):
    acrylic_3mm(lab["sheet_owner"])
    l2 = lab["cnc"]()
    vmc = lab["cnc"](profile=LASER, time_formula="A*480", with_b=False)
    already = lab["cnc"](profile=LASER, time_formula="time = max(1, B) * SLOT_DURATION_MINUTES")
    booking = egs_factory.booking(egs_factory.student(), l2, egs_factory.future(days=1), total_charge="0.00")

    out = _run("--stage", "config", "--apply", "--backup-dir", str(tmp_path / "b"))

    booking.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED
    assert CNC_REASON in booking.notes
    assert not booking.daily_slots.exists()
    assert BookingEvent.objects.filter(booking=booking, new_status=BookingStatus.CANCELLED).exists()
    for eq in (l2, vmc, already):
        eq.refresh_from_db()
        assert eq.profile_type == LASER
        assert not DynamicInputField.objects.filter(equipment=eq).exists()
        assert set(ChargeProfile.objects.filter(equipment=eq).values_list("time_formula", flat=True)) == {""}
        assert eq.supported_laser_sheet_materials.count() == 1
        assert f"preview {eq.code} cp" in out
    assert "FAILED" not in out
    assert "charge=" in out and "time=480 min" in out
    snapshot = json.loads((tmp_path / "b" / "config-before.json").read_text())
    assert len(snapshot["input_fields"]) == 5


def test_dry_run_lists_everything_and_saves_nothing(lab, egs_factory):
    acrylic_3mm(lab["sheet_owner"])
    l2 = lab["cnc"]()
    booking = egs_factory.booking(egs_factory.student(), l2, egs_factory.future(days=1), total_charge="0.00")
    p1 = lab["printer"]()
    own = print_material(p1, code="M1", price="1.5000")
    past = _past_booking(egs_factory, egs_factory.student(), p1, input_values={"A": 10, "B": "M1"})

    out = _run()

    assert "DRY RUN: nothing was saved." in out
    assert f"cancel booking {booking.pk}" in out
    assert f"delete material M1#{own.pk}" in out
    assert f"booking {past.pk} eq={p1.code}#{p1.pk} status=COMPLETED" in out and "-> delete" in out
    assert "delete input" in out
    booking.refresh_from_db()
    l2.refresh_from_db()
    assert booking.status == BookingStatus.BOOKED and l2.profile_type == HOUR
    assert PrintMaterial.objects.filter(pk=own.pk).exists() and Booking.objects.filter(pk=past.pk).exists()
    assert DynamicInputField.objects.filter(equipment=l2).count() == 2


def test_printers_end_with_master_list_only_and_old_rows_and_bookings_go(lab, egs_factory, tmp_path):
    master = [print_material(lab["print_owner"], code="PLA-FDM"), print_material(lab["print_owner"], code="ABS-FDM")]
    p1, p2 = lab["printer"](), lab["printer"]()
    m1 = print_material(p1, code="M1", price="1.5000")
    m2 = print_material(p2, code="M2", price="1.5000")
    p1.supported_print_materials.add(m2)
    for p in (p1, p2):
        p.supported_print_materials.add(*master)
    student, sub = funded_student(egs_factory)
    past = _past_booking(egs_factory, student, p1, input_values={"A": 10, "B": "M1"})
    part = print_part(p1, student, m1, booking=past)
    BookingEvent.objects.create(booking=past, event_type="STATUS_CHANGED", new_status=BookingStatus.COMPLETED)
    paid = egs_factory.booking(student, p2, egs_factory.future(days=2), input_values={"A": 10, "B": "M2"},
                               total_charge="15.00", wallet_amount_applied=Decimal("15.00"))
    sub.debit(Decimal("15.00"), description="Booking", related_user=student)
    untouched = _past_booking(egs_factory, student, p1, input_values={"A": 10, "B": "PLA-FDM"})
    stl = part.stl_file.name
    balance_before = sub.balance

    backup, out = _apply_all(tmp_path)

    sub.refresh_from_db()
    assert sub.balance == balance_before + Decimal("15.00")
    refund = SubWalletTransaction.objects.filter(sub_wallet=sub, transaction_type="credit").order_by("-pk").first()
    assert refund.amount == Decimal("15.00") and refund.description.startswith("Refund for cancelled Booking")
    assert not PrintMaterial.objects.filter(pk__in=[m1.pk, m2.pk]).exists()
    assert not Booking.objects.filter(pk__in=[past.pk, paid.pk]).exists()
    assert not PrintAnalysis.objects.filter(pk=part.pk).exists()
    assert not BookingEvent.objects.filter(booking_id__in=[past.pk, paid.pk]).exists()
    assert Booking.objects.filter(pk=untouched.pk).exists()
    for p in (p1, p2):
        assert _ids(supported_materials(p)) == sorted(m.pk for m in master)
    assert all(PrintMaterial.objects.filter(pk=m.pk).exists() for m in master)

    assert not default_storage.exists(stl)
    assert default_storage.exists(f"{ARCHIVE_PREFIX}/t1/{stl}")
    manifest = json.loads((backup / "manifest.json").read_text())
    assert sorted(b["pk"] for b in manifest["bookings"]) == sorted([past.pk, paid.pk])
    assert {b["pk"]: b["refunded"] for b in manifest["bookings"]}[paid.pk] == "15.00"
    assert (backup / "SHA256SUMS").exists() and (backup / "delete-result.json").exists()
    result = json.loads((backup / "delete-result.json").read_text())
    assert result["money_before"] == result["money_after"]
    assert "APPLIED." in out

    again = _run("--stage", "delete", "--apply", "--backup-dir", str(backup))
    assert "Nothing left to delete" in again

    restored = _run("--stage", "restore", "--apply", "--backup-dir", str(backup))
    assert "APPLIED." in restored
    assert Booking.objects.filter(pk__in=[past.pk, paid.pk]).count() == 2
    assert PrintMaterial.objects.filter(pk__in=[m1.pk, m2.pk]).count() == 2
    assert PrintAnalysis.objects.get(pk=part.pk).booking_id == past.pk
    assert default_storage.exists(stl)


def test_row_used_outside_the_lab_and_bookings_with_payment_records_are_kept(lab, egs_factory, tmp_path):
    from iic_booking.users.models.payment import DepartmentPaymentReceipt, DepartmentPaymentReceiptPurpose

    print_material(lab["print_owner"], code="PLA-FDM")
    p1 = lab["printer"]()
    shared = print_material(p1, code="C1", price="2.5000")
    lab["print_owner"].supported_print_materials.add(shared)
    gone = print_material(p1, code="M1")
    student = egs_factory.student()
    receipt_booking = _past_booking(egs_factory, student, p1, input_values={"A": 5, "B": "M1"})
    DepartmentPaymentReceipt.objects.create(
        utr_reference="UTR1", department=lab["dept"], user=student, amount=Decimal("5.00"),
        purpose=DepartmentPaymentReceiptPurpose.choices[0][0], booking=receipt_booking,
    )
    other_dept_printer = print_equipment(egs_factory, internal_department=lab["sheet_owner"].internal_department)
    outsider = _past_booking(egs_factory, student, other_dept_printer, input_values={"A": 5, "B": "M1"})
    money = money_snapshot()

    backup, out = _apply_all(tmp_path)

    assert PrintMaterial.objects.filter(pk=shared.pk).exists()
    assert not PrintMaterial.objects.filter(pk=gone.pk).exists()
    assert Booking.objects.filter(pk__in=[receipt_booking.pk, outsider.pk]).count() == 2
    assert DepartmentPaymentReceipt.objects.filter(booking=receipt_booking).exists()
    manifest = json.loads((backup / "manifest.json").read_text())
    assert "DepartmentPaymentReceipt" in manifest["blocked"][str(receipt_booking.pk)]
    assert manifest["kept_rows"][0]["pk"] == shared.pk
    assert money_snapshot() == money


def test_delete_refuses_when_the_plan_changed_since_the_backup(lab, egs_factory, tmp_path):
    print_material(lab["print_owner"], code="PLA-FDM")
    p1 = lab["printer"]()
    print_material(p1, code="M1")
    backup = tmp_path / "b"
    _run("--stage", "config", "--apply")
    _run("--stage", "backup", "--backup-dir", str(backup))
    late = _past_booking(egs_factory, egs_factory.student(), p1, input_values={"A": 5, "B": "M1"})

    with pytest.raises(CommandError, match="changed since the backup"):
        _run("--stage", "delete", "--apply", "--backup-dir", str(backup))
    assert Booking.objects.filter(pk=late.pk).exists()
