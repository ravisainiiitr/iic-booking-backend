"""3D print actual weight / time re-price the booking through the post-booking charge recalculation, even when
the equipment's 'enable_charge_recalculation' switch is off (production 3D printers have it off)."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.fabrication import inject_print_parts, strip_fabrication_keys
from iic_booking.equipment.models import BookingEvent, BookingEventType, BookingStatus, EquipmentManager
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import funded_student, print_equipment, print_material, print_part


@pytest.fixture(autouse=True)
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


def _setup(egs_factory, *, files=1, status=BookingStatus.BOOKED):
    """Estimate per file: ceil(10.2) = 11 g x 1.44/g = 15.84 + 30 min at 60/h = 30 -> 45.84."""
    eq = print_equipment(egs_factory, hourly_rate="60.00")
    eq.enable_charge_recalculation = False
    eq.save(update_fields=["enable_charge_recalculation"])
    owner, wallet = funded_student(egs_factory)
    pla = print_material(eq)
    parts = [
        print_part(eq, owner, pla, weight="10.2", minutes=30, name=f"part{i}", sequence=i) for i in range(files)
    ]
    estimate = {1: "46.00", 2: "92.00"}[files]
    booking = egs_factory.booking(
        owner,
        eq,
        egs_factory.future(),
        input_values=strip_fabrication_keys(inject_print_parts({}, parts)),
        total_charge=estimate,
        print_analysis=parts[0],
    )
    for p in parts:
        p.booking = booking
        p.save(update_fields=["booking"])
    if status != BookingStatus.BOOKED:
        booking.status = status
        booking.completed_at = timezone.now() if status == BookingStatus.COMPLETED else None
        booking.save(update_fields=["status", "completed_at"])
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return eq, owner, wallet, oic, booking, parts


def _set_actuals(egs_factory, user, booking, **body):
    return egs_factory.client_for(user).patch(f"/api/bookings/{booking.pk}/print-actuals/", body, format="json")


@pytest.mark.django_db
def test_higher_actuals_raise_the_charge_and_post_an_extra_amount(egs_factory):
    _eq, _owner, wallet, oic, booking, parts = _setup(egs_factory)
    balance = wallet.balance

    resp = _set_actuals(egs_factory, oic, booking, actual_weight_grams=40, actual_time_minutes=60)
    assert resp.status_code == 200, resp.data

    booking.refresh_from_db()
    # 40 g x 1.44 = 57.60 + 60 min at 60/h = 60 -> 117.60 -> 118
    assert booking.total_charge == Decimal("118.00")
    assert booking.charge_recalculation_pending_amount == Decimal("72.00")
    assert booking.total_time_minutes == 60
    assert (booking.input_values["A"], booking.input_values["C"]) == (40, 60)
    assert Decimal(resp.data["charge_recalculation_summary"]["extra_amount"]) == Decimal("72")
    assert Decimal(resp.data["booking"]["charge_recalculation_pending_amount"]) == Decimal("72")

    event = BookingEvent.objects.get(booking=booking, event_type=BookingEventType.CHARGE_RECALCULATED)
    assert Decimal(event.metadata["previous_charge"]) == Decimal("46")
    assert Decimal(event.metadata["new_charge"]) == Decimal("118")
    assert Decimal(event.metadata["extra_amount"]) == Decimal("72")

    wallet.refresh_from_db()
    assert wallet.balance == balance, "the extra amount is collected with Deduct Money / Pay Now, not silently"

    pay = egs_factory.client_for(oic).post(f"/api/bookings/{booking.pk}/process-charge-recalculation-pay-now/", {}, format="json")
    assert pay.status_code == 200, pay.data
    wallet.refresh_from_db()
    booking.refresh_from_db()
    assert wallet.balance == balance - Decimal("72.00")
    assert booking.charge_recalculation_pending_amount is None


@pytest.mark.django_db
def test_lower_actuals_become_a_refund_the_oic_confirms(egs_factory):
    _eq, owner, wallet, oic, booking, _parts = _setup(egs_factory)
    balance = wallet.balance

    resp = _set_actuals(egs_factory, oic, booking, actual_weight_grams=5, actual_time_minutes=10)
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    # 5 g x 1.44 = 7.20 + 10 min at 60/h = 10 -> 17.20 -> 17
    assert booking.total_charge == Decimal("17.00")
    assert booking.charge_recalculation_pending_amount == Decimal("-29.00")
    assert resp.data["charge_recalculation_summary"]["refund_status"] == "awaiting_oic_confirmation"
    wallet.refresh_from_db()
    assert wallet.balance == balance

    url = f"/api/bookings/{booking.pk}/process-charge-recalculation-refund/"
    assert egs_factory.client_for(owner).post(url, {}, format="json").status_code == 403
    confirm = egs_factory.client_for(oic).post(url, {}, format="json")
    assert confirm.status_code == 200, confirm.data
    wallet.refresh_from_db()
    assert wallet.balance == balance + Decimal("29.00")


@pytest.mark.django_db
def test_actuals_on_a_completed_booking_need_the_oic(egs_factory):
    _eq, _owner, _wallet, oic, booking, _parts = _setup(egs_factory, status=BookingStatus.COMPLETED)
    other_oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)

    assert _set_actuals(egs_factory, other_oic, booking, actual_weight_grams=40, actual_time_minutes=60).status_code == 403
    booking.refresh_from_db()
    assert booking.total_charge == Decimal("46.00")

    resp = _set_actuals(egs_factory, oic, booking, actual_weight_grams=40, actual_time_minutes=60)
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.total_charge == Decimal("118.00")
    assert booking.charge_recalculation_pending_amount == Decimal("72.00")


@pytest.mark.django_db
def test_actuals_of_one_file_only_replace_that_files_share(egs_factory):
    _eq, _owner, _wallet, oic, booking, parts = _setup(egs_factory, files=2)

    resp = _set_actuals(
        egs_factory, oic, booking, analysis_id=str(parts[1].id), actual_weight_grams=40, actual_time_minutes=60
    )
    assert resp.status_code == 200, resp.data
    parts[0].refresh_from_db()
    parts[1].refresh_from_db()
    assert parts[0].actual_weight_grams is None
    assert parts[1].actual_weight_grams == Decimal("40")
    booking.refresh_from_db()
    # file 1 estimate 11 g / 30 min + file 2 actual 40 g / 60 min = 51 g / 90 min
    assert (booking.input_values["A"], booking.input_values["C"]) == (51, 90)
    # 51 x 1.44 = 73.44 + 90 min at 60/h = 90 -> 163.44 -> 163
    assert booking.total_charge == Decimal("163.00")
    assert booking.charge_recalculation_pending_amount == Decimal("71.00")


@pytest.mark.django_db
def test_resaving_actuals_corrects_a_charge_that_was_never_adjusted(egs_factory):
    _eq, _owner, _wallet, oic, booking, parts = _setup(egs_factory)
    parts[0].actual_weight_grams = Decimal("40")
    parts[0].actual_time_minutes = 60
    parts[0].save(update_fields=["actual_weight_grams", "actual_time_minutes"])

    resp = _set_actuals(egs_factory, oic, booking, actual_weight_grams=40, actual_time_minutes=60)
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.total_charge == Decimal("118.00")
    assert booking.charge_recalculation_pending_amount == Decimal("72.00")


@pytest.mark.django_db
def test_actuals_rejected_for_refunded_booking_and_foreign_file(egs_factory):
    _eq, _owner, _wallet, oic, booking, _parts = _setup(egs_factory)

    bad = _set_actuals(
        egs_factory, oic, booking, analysis_id="00000000-0000-0000-0000-000000000000", actual_weight_grams=40
    )
    assert bad.status_code == 400

    booking.status = BookingStatus.REFUNDED
    booking.save(update_fields=["status"])
    assert _set_actuals(egs_factory, oic, booking, actual_weight_grams=40).status_code == 400
    booking.refresh_from_db()
    assert booking.total_charge == Decimal("46.00")
    assert not BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.CHARGE_RECALCULATED).exists()
