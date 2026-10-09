"""fix_midnight_booking_minutes: bookings on slots extended to midnight store the slot length, nothing else."""

from datetime import timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.core.management import call_command
from django.db.models.signals import post_save, pre_save
from django.utils import timezone

from iic_booking.equipment.management.commands.fix_midnight_booking_minutes import fix_midnight_booking_minutes
from iic_booking.equipment.models import Booking, BookingStatus


def _booking(egs, eq, student, start, *, slots, stored, status=BookingStatus.BOOKED, **fields):
    booking = egs.booking(student, eq, start, slot_count=0, total_charge="500.00", **fields)
    for offset, minutes in slots:
        egs.slot(eq, start + timedelta(minutes=offset), minutes=minutes, status="BOOKED", booking=booking)
    Booking.objects.filter(pk=booking.pk).update(total_time_minutes=stored, status=status)
    return booking


def _minutes(booking):
    return Booking.objects.values_list("total_time_minutes", flat=True).get(pk=booking.pk)


@pytest.fixture
def setup(egs_factory):
    egs = egs_factory
    full_day = egs.equipment(code="NMR TXI")
    half_day = egs.equipment(code="SQUID")
    other = egs.equipment(code="XRD")
    student = egs.student()
    day = egs.future(days=3, hour=0)
    noon = day + timedelta(hours=12)
    return {
        "ids": [full_day.pk, half_day.pk],
        "student": student,
        "full": _booking(egs, full_day, student, day, slots=[(0, 1440)], stored=1439,
                         status=BookingStatus.COMPLETED),
        "two_days": _booking(egs, full_day, student, day + timedelta(days=1), slots=[(0, 1440), (1440, 1440)],
                             stored=2878),
        "half": _booking(egs, half_day, student, noon, slots=[(0, 720)], stored=719,
                         status=BookingStatus.CANCELLED),
        "ok": _booking(egs, full_day, student, day + timedelta(days=4), slots=[(0, 1440)], stored=1440),
        "formula": _booking(egs, half_day, student, noon + timedelta(days=1), slots=[(0, 720)], stored=60),
        "editing": _booking(
            egs, half_day, student, noon + timedelta(days=2), slots=[(0, 720)], stored=719,
            charge_recalculation_pay_deadline=timezone.now() + timedelta(seconds=60),
            charge_recalculation_revert_snapshot={"total_time_minutes": 719},
        ),
        "not_listed": _booking(egs, other, student, day, slots=[(0, 1440)], stored=1439),
        "not_midnight": _booking(egs, half_day, student, day + timedelta(days=6, hours=9), slots=[(0, 60)],
                                 stored=59),
    }


@pytest.mark.django_db
def test_dry_run_reports_without_writing(setup):
    result = fix_midnight_booking_minutes(apply=False, equipment_ids=setup["ids"])

    assert {r["booking_id"]: (r["old"], r["new"]) for r in result["fixes"]} == {
        setup["full"].pk: (1439, 1440),
        setup["two_days"].pk: (2878, 2880),
        setup["half"].pk: (719, 720),
    }
    assert {r["booking_id"]: r["reason"] for r in result["skipped"]} == {
        setup["formula"].pk: "difference_not_from_midnight_ends",
        setup["editing"].pk: "input_edit_payment_window",
    }
    assert result["updated"] == 0
    assert result["remaining_to_fix"] == 3
    assert _minutes(setup["full"]) == 1439
    nmr = result["per_equipment"]["NMR TXI"]
    assert (nmr["bookings"], nmr["to_fix"], nmr["already_ok"]) == (3, 2, 1)
    assert nmr["changes"] == {"1439->1440": 1, "2878->2880": 1}


@pytest.mark.django_db
def test_apply_writes_only_the_length_without_save_or_signals(setup):
    keys = ("full", "two_days", "half", "ok", "formula", "editing", "not_listed", "not_midnight")
    before = {
        k: Booking.objects.filter(pk=setup[k].pk)
        .values("total_charge", "amount_due", "wallet_amount_applied", "status", "charge_breakdown", "updated_at")
        .get()
        for k in keys
    }
    sent = []

    def _record(sender, **kwargs):
        sent.append(sender)

    pre_save.connect(_record, sender=Booking, weak=False)
    post_save.connect(_record, sender=Booking, weak=False)
    try:
        result = fix_midnight_booking_minutes(apply=True, equipment_ids=setup["ids"])
    finally:
        pre_save.disconnect(_record, sender=Booking)
        post_save.disconnect(_record, sender=Booking)

    assert sent == []
    assert result["updated"] == 3
    assert result["charges_unchanged"] is True
    assert result["remaining_to_fix"] == 0
    expected = {"full": 1440, "two_days": 2880, "half": 720, "ok": 1440, "formula": 60, "editing": 719,
                "not_listed": 1439, "not_midnight": 59}
    assert {k: _minutes(setup[k]) for k in keys} == expected
    for k in keys:
        after = Booking.objects.filter(pk=setup[k].pk).values(*before[k]).get()
        assert after == before[k], k
    assert before["full"]["total_charge"] == Decimal("500.00")

    again = fix_midnight_booking_minutes(apply=True, equipment_ids=setup["ids"])
    assert (again["updated"], again["fixes"], again["remaining_to_fix"]) == (0, [], 0)


@pytest.mark.django_db
def test_command_output_has_ids_and_minutes_only(setup):
    out = StringIO()
    call_command("fix_midnight_booking_minutes", "--apply", *[f"--equipment-id={i}" for i in setup["ids"]],
                 stdout=out)
    text = out.getvalue()
    assert "mode=APPLY" in text
    assert "eq=NMR TXI" in text and "changes=1439->1440:1,2878->2880:1" in text
    assert f"booking_id={setup['full'].pk}" in text and "old=1439 new=1440" in text
    assert "charges_unchanged=True" in text
    assert "remaining_to_fix=0" in text
    student = setup["student"]
    assert student.email not in text
    if student.name:
        assert student.name not in text
