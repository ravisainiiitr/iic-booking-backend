"""Refunds use the amount actually paid while a charge-recalculation difference is still open.

Booking IICTEST-3DP-01202600002: paid ₹500, OIC actuals cut the charge to ₹144 (refund ₹356 awaiting the
OIC), files then replaced at ₹418 — the paid ₹500 stays the baseline and the open refund becomes ₹82.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.equipment import api_views, booking_events, fabrication_workflow, print_3d_notifications
from iic_booking.equipment.booking_cancellation import perform_booking_cancellation
from iic_booking.equipment.booking_paid_amount import (
    booking_paid_charge,
    drop_unpaid_extra,
    net_refund_against_unpaid_extra,
)
from iic_booking.equipment.fabrication_workflow import expire_fabrication_rejections
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    DailySlot,
    EquipmentManager,
    EquipmentOperator,
    LaserCutBatch,
)
from iic_booking.equipment.operator_unavailable import apply_operator_unavailable_booking
from iic_booking.equipment.serializers import BookingListSerializer, BookingSerializer
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .fabrication_helpers import acrylic_3mm, funded_student, laser_equipment, laser_part

REASON = "The inner walls are thinner than the laser kerf."


def _b(total, pending=None):
    return SimpleNamespace(
        total_charge=Decimal(total),
        charge_recalculation_pending_amount=Decimal(pending) if pending is not None else None,
        charge_recalculation_pay_deadline=None,
        charge_recalculation_revert_snapshot=None,
    )


# --------------------------------------------------------------------------- helper


@pytest.mark.parametrize(
    ("total", "pending", "paid"),
    [
        ("418.00", None, "418.00"),
        ("418.00", "0.00", "418.00"),
        ("418.00", "-82.00", "500.00"),  # refund awaiting the OIC: money still held
        ("418.00", "274.00", "144.00"),  # extra not yet collected
        ("100.00", "150.00", "0.00"),
    ],
)
def test_booking_paid_charge(total, pending, paid):
    assert booking_paid_charge(_b(total, pending)) == Decimal(paid)


def test_partial_refund_settles_an_unpaid_extra_first():
    assert net_refund_against_unpaid_extra(_b("418.00", "274.00"), Decimal("100.00")) == (Decimal("0.00"), Decimal("174.00"))
    assert net_refund_against_unpaid_extra(_b("418.00", "274.00"), Decimal("300.00")) == (Decimal("26.00"), None)
    assert net_refund_against_unpaid_extra(_b("418.00", "-82.00"), Decimal("100.00")) == (Decimal("100.00"), Decimal("-82.00"))
    assert net_refund_against_unpaid_extra(_b("418.00"), Decimal("100.00")) == (Decimal("100.00"), None)


def test_cancel_without_refund_forgets_only_an_unpaid_extra():
    extra = _b("418.00", "274.00")
    assert drop_unpaid_extra(extra) and extra.charge_recalculation_pending_amount is None
    refund = _b("418.00", "-82.00")
    assert drop_unpaid_extra(refund) == []
    assert refund.charge_recalculation_pending_amount == Decimal("-82.00")


# --------------------------------------------------------------------------- integration


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(fabrication_workflow, "_send_user_email", lambda *a, **k: None)
    monkeypatch.setattr(print_3d_notifications, "dispatch_fabrication_file_email", lambda *a, **k: None)
    monkeypatch.setattr(booking_events, "_dispatch_booking_event_notification", lambda *a, **k: None)


@pytest.fixture
def lab(egs_factory, media_tmp, quiet):
    eq = laser_equipment(egs_factory, emails=["laser-lab@example.com"])
    acr = acrylic_3mm(eq)
    student, sub = funded_student(egs_factory)
    booking = egs_factory.booking(student, eq, egs_factory.future(), total_charge="418.00")
    batch = LaserCutBatch.objects.create(equipment=eq, user=student, booking=booking, status="COMPLETED")
    laser_part(eq, student, acr, quantity=5, name="old", batch=batch, booking=booking)
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    return SimpleNamespace(factory=egs_factory, eq=eq, student=student, sub=sub, booking=booking, oic=oic,
                           operator=operator, admin=admin)


def _pending(booking, amount):
    Booking.objects.filter(pk=booking.pk).update(charge_recalculation_pending_amount=Decimal(amount))
    booking.refresh_from_db()


def _balance(sub):
    sub.refresh_from_db()
    return Decimal(str(sub.balance))


@pytest.mark.django_db
@pytest.mark.parametrize(("pending", "refund"), [("-82.00", "500.00"), ("274.00", "144.00"), (None, "418.00")])
def test_expired_rejection_refunds_what_was_paid(lab, pending, refund, django_capture_on_commit_callbacks):
    if pending is not None:
        _pending(lab.booking, pending)
    deadline = timezone.now() - timedelta(minutes=1)
    Booking.objects.filter(pk=lab.booking.pk).update(
        fabrication_rejected_at=deadline - timedelta(hours=24),
        fabrication_replace_deadline=deadline,
        fabrication_rejection_reason=REASON,
    )
    before = _balance(lab.sub)
    with django_capture_on_commit_callbacks(execute=True):
        assert expire_fabrication_rejections() == 1

    booking = Booking.objects.get(pk=lab.booking.pk)
    assert booking.status == BookingStatus.REFUNDED
    assert booking.charge_recalculation_pending_amount is None
    assert _balance(lab.sub) - before == Decimal(refund)
    event = BookingEvent.objects.get(booking=booking, event_type=BookingEventType.REFUNDED)
    assert event.metadata["refund_amount"] == refund
    (lab_mail,) = [m for m in mail.outbox if "laser-lab@example.com" in m.to]
    assert f"₹{Decimal(refund):,.2f}" in lab_mail.body

    # The OIC's "Confirm refund" can no longer pay the old difference a second time.
    resp = lab.factory.client_for(lab.oic).post(
        f"/api/bookings/{booking.pk}/process-charge-recalculation-refund/", {}, format="json"
    )
    assert resp.status_code == 400
    assert _balance(lab.sub) - before == Decimal(refund)


@pytest.mark.django_db
def test_user_cancel_refunds_paid_amount_and_serializers_expose_it(lab):
    _pending(lab.booking, "-82.00")
    assert BookingSerializer(lab.booking, context={"request": None}).data["amount_paid"] == "500.00"
    assert BookingListSerializer(lab.booking, context={"request": None}).data["amount_paid"] == "500.00"
    deadline = timezone.now() + timedelta(hours=3)
    Booking.objects.filter(pk=lab.booking.pk).update(
        fabrication_rejected_at=timezone.now(), fabrication_replace_deadline=deadline, fabrication_rejection_reason=REASON
    )
    before = _balance(lab.sub)
    resp = lab.factory.client_for(lab.student).post(f"/api/bookings/{lab.booking.pk}/user-cancel/", {}, format="json")
    assert resp.status_code == 200, resp.data
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.REFUNDED
    assert lab.booking.charge_recalculation_pending_amount is None
    assert _balance(lab.sub) - before == Decimal("500.00")


@pytest.mark.django_db
def test_admin_refund_uses_paid_amount(lab):
    _pending(lab.booking, "274.00")
    before = _balance(lab.sub)
    resp = lab.factory.client_for(lab.admin).post(f"/api/bookings/{lab.booking.pk}/refund/", {}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["refund_amount"] == "144.00"
    assert _balance(lab.sub) - before == Decimal("144.00")
    lab.booking.refresh_from_db()
    assert lab.booking.charge_recalculation_pending_amount is None


@pytest.mark.django_db
def test_operator_unavailable_refunds_paid_amount(lab):
    _pending(lab.booking, "-82.00")
    before = _balance(lab.sub)
    booking = Booking.objects.select_related("equipment", "user").get(pk=lab.booking.pk)
    apply_operator_unavailable_booking(booking, actor=lab.oic)
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.ABSENT
    assert lab.booking.charge_recalculation_pending_amount is None
    assert _balance(lab.sub) - before == Decimal("500.00")


@pytest.mark.django_db
def test_cancel_without_refund_keeps_a_refund_awaiting_the_oic(lab):
    _pending(lab.booking, "-82.00")
    slot_ids = list(DailySlot.objects.filter(booking=lab.booking).values_list("id", flat=True))
    before = _balance(lab.sub)
    perform_booking_cancellation(
        lab.booking, slot_ids=slot_ids, should_refund=False, cancel_notes="", actor=lab.oic,
        allow_started_slots=True, cancelled_by_label="admin",
    )
    lab.booking.refresh_from_db()
    assert lab.booking.status == BookingStatus.CANCELLED
    assert lab.booking.charge_recalculation_pending_amount == Decimal("-82.00")
    assert _balance(lab.sub) == before


# --------------------------------------------------------------------------- recalculation baseline


def _recalculate(monkeypatch, lab, new_charge, actor):
    monkeypatch.setattr(
        api_views.ChargeCalculationEngine, "calculate_charge",
        staticmethod(lambda *a, **k: (Decimal(new_charge), [{"description": "Part", "amount": float(new_charge)}])),
    )
    booking = Booking.objects.select_related("equipment", "charge_profile", "user").get(pk=lab.booking.pk)
    return api_views._recalculate_booking_charge_and_adjust_wallet(SimpleNamespace(user=actor), booking)


@pytest.mark.django_db
def test_replace_while_refund_awaits_oic_uses_amount_paid(lab, monkeypatch):
    Booking.objects.filter(pk=lab.booking.pk).update(total_charge=Decimal("500.00"))
    first = _recalculate(monkeypatch, lab, "144.00", lab.oic)
    assert (Decimal(first["previous_charge"]), Decimal(first["refund_amount"])) == (500, 356)

    second = _recalculate(monkeypatch, lab, "418.00", lab.student)
    assert (Decimal(second["previous_charge"]), Decimal(second["refund_amount"])) == (500, 82)
    lab.booking.refresh_from_db()
    assert lab.booking.charge_recalculation_pending_amount == Decimal("-82.00")
    event = BookingEvent.objects.filter(booking=lab.booking, event_type=BookingEventType.CHARGE_RECALCULATED).latest(
        "created_at"
    )
    assert "earlier refund of ₹356.00 had not been approved yet" in event.comment
    assert Decimal(event.metadata["replaced_pending_amount"]) == -356


@pytest.mark.django_db
def test_replace_after_refund_was_paid_asks_for_the_difference(lab, monkeypatch):
    Booking.objects.filter(pk=lab.booking.pk).update(total_charge=Decimal("500.00"))
    _recalculate(monkeypatch, lab, "144.00", lab.oic)
    before = _balance(lab.sub)
    resp = lab.factory.client_for(lab.oic).post(
        f"/api/bookings/{lab.booking.pk}/process-charge-recalculation-refund/", {}, format="json"
    )
    assert resp.status_code == 200, resp.data
    assert _balance(lab.sub) - before == Decimal("356.00")

    summary = _recalculate(monkeypatch, lab, "418.00", lab.student)
    assert Decimal(summary["previous_charge"]) == 144
    assert Decimal(summary["extra_amount"]) == 274
    assert summary["refund_amount"] is None
