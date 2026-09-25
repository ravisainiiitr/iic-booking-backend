"""
Equipment Group business rules: single-department groups and same charge across members.

Department rule: every group-membership write path rejects cross-department groups, and a
cross-equipment reschedule defensively rejects a target from another department.
Pricing rule: a cross-equipment reschedule never changes the charge, wallet, settlement
department or virtual booking id; cancellations afterwards behave exactly like a booking that
never moved.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.test import RequestFactory

from iic_booking.equipment import booking_cancellation as bc
from iic_booking.equipment import equipment_group_service as egs
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    ChargeProfile,
    Equipment,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWalletTransaction,
    Wallet,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)
from iic_booking.users.repositories.wallet_repository import SubWalletRepository, WalletRepository
from iic_booking.users.tests.factories import UserFactory

MESSAGE = "All equipment in an Equipment Group must belong to the same department."


def _other_department():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"EGS-Other-{tag}", code=f"EO{tag[:4]}", equipment_booking_enabled=True, equipment_visibility_enabled=True
    )


def _main_admin():
    return UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)


def _members(group):
    return set(Equipment.objects.filter(equipment_group=group).values_list("pk", flat=True))


# ---------------------------------------------------------------------------
# Department rule: group administration
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_api_group_with_same_department_equipment_passes(egs_factory):
    a, b, c = (egs_factory.equipment() for _ in range(3))
    client = egs_factory.client_for(_main_admin())

    created = client.post("/api/admin/equipment-groups/", {"name": "Same Dept", "code": "SAME-DEPT"}, format="json")
    assert created.status_code == 201, created.data
    group_id = created.data["equipment_group_id"]
    url = f"/api/admin/equipment-groups/{group_id}/"

    res = client.put(url, {"name": "Same Dept", "code": "SAME-DEPT", "equipment_ids": [a.pk, b.pk]}, format="json")
    assert res.status_code == 200, res.data
    assert _members(group_id) == {a.pk, b.pk}

    replaced = client.put(url, {"name": "Same Dept", "code": "SAME-DEPT", "equipment_ids": [b.pk, c.pk]}, format="json")
    assert replaced.status_code == 200, replaced.data
    assert _members(group_id) == {b.pk, c.pk}


@pytest.mark.django_db
def test_admin_api_update_to_cross_department_is_rejected_without_changes(egs_factory):
    group = egs_factory.group()
    a = egs_factory.equipment(group)
    b = egs_factory.equipment(group)
    foreign = egs_factory.equipment(internal_department=_other_department())
    url = f"/api/admin/equipment-groups/{group.pk}/"

    res = egs_factory.client_for(_main_admin()).put(
        url, {"name": "Renamed", "code": group.code, "equipment_ids": [a.pk, b.pk, foreign.pk]}, format="json"
    )

    assert res.status_code == 400
    assert res.data["error"] == MESSAGE
    group.refresh_from_db()
    assert group.name != "Renamed"
    assert _members(group) == {a.pk, b.pk}
    foreign.refresh_from_db()
    assert foreign.equipment_group_id is None


@pytest.mark.django_db
def test_admin_api_create_cannot_attach_cross_department_equipment(egs_factory):
    a = egs_factory.equipment()
    foreign = egs_factory.equipment(internal_department=_other_department())

    res = egs_factory.client_for(_main_admin()).post(
        "/api/admin/equipment-groups/",
        {"name": "Bypass", "code": "BYPASS-1", "equipment_ids": [a.pk, foreign.pk]},
        format="json",
    )

    assert res.status_code == 201, res.data
    assert _members(res.data["equipment_group_id"]) == set()


@pytest.mark.django_db
def test_equipment_api_bypass_is_rejected(egs_factory):
    group = egs_factory.group()
    member = egs_factory.equipment(group)
    egs_factory.equipment(group)
    foreign = egs_factory.equipment(internal_department=_other_department())
    client = egs_factory.client_for(_main_admin())

    joined = client.patch(f"/api/admin/equipment/{foreign.pk}/", {"equipment_group": group.pk}, format="json")
    assert joined.status_code == 400
    assert MESSAGE in str(joined.data)
    foreign.refresh_from_db()
    assert foreign.equipment_group_id is None

    moved = client.patch(
        f"/api/admin/equipment/{member.pk}/", {"internal_department": foreign.internal_department_id}, format="json"
    )
    assert moved.status_code == 400
    assert MESSAGE in str(moved.data)
    member.refresh_from_db()
    assert member.internal_department_id == egs_factory.department.pk


@pytest.mark.django_db
def test_equipment_serializer_accepts_same_department_assignment(egs_factory):
    from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer

    group = egs_factory.group()
    egs_factory.equipment(group)
    newcomer = egs_factory.equipment()
    foreign = egs_factory.equipment(internal_department=_other_department())

    ok = EquipmentAdminWriteSerializer(instance=newcomer, data={"equipment_group": group.pk}, partial=True)
    assert ok.is_valid(), ok.errors
    bad = EquipmentAdminWriteSerializer(instance=foreign, data={"equipment_group": group.pk}, partial=True)
    assert not bad.is_valid()
    assert bad.errors["equipment_group"] == [MESSAGE]


def _admin_request(path="/admin/"):
    from django.contrib.messages.storage.fallback import FallbackStorage

    request = RequestFactory().post(path, {})
    request.user = _main_admin()
    request.session = "session"
    request._messages = FallbackStorage(request)
    return request


@pytest.mark.django_db
def test_django_admin_bulk_assign_rejects_cross_department(egs_factory):
    from django.contrib.admin.sites import site

    group = egs_factory.group()
    member = egs_factory.equipment(group)
    same = egs_factory.equipment()
    foreign = egs_factory.equipment(internal_department=_other_department())
    equipment_admin = site._registry[Equipment]

    request = _admin_request()
    request.POST = request.POST.copy()
    request.POST["equipment_group"] = str(group.pk)
    equipment_admin.assign_to_group(request, Equipment.objects.filter(pk__in=[same.pk, foreign.pk]))
    assert _members(group) == {member.pk}
    assert MESSAGE in [str(m) for m in request._messages]

    request = _admin_request()
    request.POST = request.POST.copy()
    request.POST["equipment_group"] = str(group.pk)
    equipment_admin.assign_to_group(request, Equipment.objects.filter(pk=same.pk))
    assert _members(group) == {member.pk, same.pk}


def _inline_formset(group, data_rows):
    from django.contrib.admin.sites import site

    from iic_booking.equipment.admin import EquipmentInline
    from iic_booking.equipment.models import EquipmentGroup

    request = RequestFactory().get("/admin/")
    request.user = _main_admin()
    assert EquipmentInline in site._registry[EquipmentGroup].inlines
    inline = EquipmentInline(EquipmentGroup, site)
    # Admin inline forms report "unchanged" without model permissions; grant them for the form under test.
    for perm in ("has_add_permission", "has_change_permission", "has_delete_permission", "has_view_permission"):
        setattr(inline, perm, lambda *args, **kwargs: True)
    formset_class = inline.get_formset(request, group)
    prefix = formset_class.get_default_prefix()
    existing = [r for r in data_rows if "existing" in r]
    data = {
        f"{prefix}-TOTAL_FORMS": str(len(data_rows)),
        f"{prefix}-INITIAL_FORMS": str(len(existing)),
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
    }
    for i, row in enumerate(data_rows):
        if "existing" in row:
            data[f"{prefix}-{i}-equipment_id"] = str(row["existing"].pk)
            data[f"{prefix}-{i}-equipment_group"] = str(group.pk)
        else:
            data[f"{prefix}-{i}-equipment_select"] = str(row["select"].pk)
    return formset_class(data=data, instance=group, prefix=prefix)


@pytest.mark.django_db
def test_django_admin_group_inline_enforces_department(egs_factory):
    group = egs_factory.group()
    member = egs_factory.equipment(group)
    same = egs_factory.equipment()
    foreign = egs_factory.equipment(internal_department=_other_department())

    bad = _inline_formset(group, [{"existing": member}, {"select": foreign}])
    assert not bad.is_valid()
    assert MESSAGE in bad.non_form_errors()

    good = _inline_formset(group, [{"existing": member}, {"select": same}])
    assert good.is_valid(), (good.errors, good.non_form_errors())


@pytest.mark.django_db
def test_existing_groups_are_not_modified_and_stay_editable(egs_factory):
    """Legacy cross-department data is reported, never auto-fixed; unrelated edits still work."""
    group = egs_factory.group()
    a = egs_factory.equipment(group)
    legacy = egs_factory.equipment(group)
    Equipment.objects.filter(pk=legacy.pk).update(internal_department=_other_department())
    url = f"/api/admin/equipment-groups/{group.pk}/"
    client = egs_factory.client_for(_main_admin())

    renamed = client.put(url, {"name": "Renamed", "code": group.code, "equipment_ids": [a.pk, legacy.pk]}, format="json")
    assert renamed.status_code == 200, renamed.data
    assert _members(group) == {a.pk, legacy.pk}

    newcomer = egs_factory.equipment()
    grown = client.put(
        url, {"name": "Renamed", "code": group.code, "equipment_ids": [a.pk, legacy.pk, newcomer.pk]}, format="json"
    )
    assert grown.status_code == 400
    assert _members(group) == {a.pk, legacy.pk}

    fixed = client.put(url, {"name": "Renamed", "code": group.code, "equipment_ids": [a.pk, newcomer.pk]}, format="json")
    assert fixed.status_code == 200, fixed.data
    assert _members(group) == {a.pk, newcomer.pk}


# ---------------------------------------------------------------------------
# Pricing / settlement / virtual_booking_id across cross-equipment rescheduling
# ---------------------------------------------------------------------------


def _funded_wallet(owner, department, amount):
    faculty = UserFactory(user_type=UserType.FACULTY, department=department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=owner, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    sub = SubWalletRepository.get_or_create(wallet, department)
    sub.credit(amount, description="Recharge")
    return sub


def _paid_booking(egs_factory, owner, equipment, start, *, amount, slot_count=1):
    booking = egs_factory.booking(
        owner,
        equipment,
        start,
        total_charge=amount,
        slot_count=slot_count,
        amount_due=Decimal(amount),
        settlement_department=egs_factory.department,
    )
    sub, _ = WalletRepository.get_booking_wallet_target(owner, equipment.internal_department)
    sub.debit(Decimal(amount), description=f"Booking {booking.virtual_booking_id}", related_user=owner)
    return booking


def _reschedule(egs_factory, owner, booking, target, slots):
    return egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {
            "start_time": slots[0].start_datetime.isoformat(),
            "end_time": slots[-1].end_datetime.isoformat(),
            "target_equipment_id": target.pk,
        },
        format="json",
    )


def _free_slots(egs_factory, equipment, start, count=1):
    from datetime import timedelta

    return [egs_factory.slot(equipment, start + timedelta(minutes=60 * i)) for i in range(count)]


@pytest.fixture
def quiet_cancellation(monkeypatch):
    real = bc.create_booking_event

    def _event(**kwargs):
        kwargs["send_notification"] = False
        return real(**kwargs)

    monkeypatch.setattr(bc, "create_booking_event", _event)
    monkeypatch.setattr(bc, "schedule_waitlist_slots_available_after_commit", lambda *a, **k: None)


def _cancel(booking, owner, slot_ids, partial_plan=None):
    return bc.perform_booking_cancellation(
        booking,
        slot_ids=slot_ids,
        should_refund=True,
        cancel_notes="",
        actor=owner,
        allow_started_slots=False,
        cancelled_by_label="user",
        partial_plan=partial_plan,
    )


def _money_snapshot(booking, sub):
    booking.refresh_from_db()
    sub.refresh_from_db()
    return {
        "total_charge": booking.total_charge,
        "amount_due": booking.amount_due,
        "settlement_department_id": booking.settlement_department_id,
        "virtual_booking_id": booking.virtual_booking_id,
        "balance": sub.balance,
        "transactions": SubWalletTransaction.objects.filter(sub_wallet=sub).count(),
    }


@pytest.mark.django_db
def test_reschedule_between_same_charge_members_keeps_every_financial_field(
    egs_factory, egs_flags_on, egs_quiet_side_effects
):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    a = egs_factory.equipment(group, unit_charge="1000.00")
    b = egs_factory.equipment(group, unit_charge="1000.00")
    owner = egs_factory.student()
    sub = _funded_wallet(owner, egs_factory.department, "5000.00")
    booking = _paid_booking(egs_factory, owner, a, egs_factory.future(days=4, hour=10), amount="1000.00")
    before = _money_snapshot(booking, sub)
    assert before["balance"] == Decimal("4000.00")
    all_txns = SubWalletTransaction.objects.count()

    slots = _free_slots(egs_factory, b, egs_factory.future(days=5, hour=11))
    res = _reschedule(egs_factory, owner, booking, b, slots)

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.equipment_id == b.pk
    assert booking.charge_profile.equipment_id == b.pk
    assert _money_snapshot(booking, sub) == before
    assert SubWalletTransaction.objects.count() == all_txns

    event = BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.RESCHEDULED).latest("pk")
    meta = event.metadata
    assert (meta["previous_equipment_id"], meta["new_equipment_id"]) == (a.pk, b.pk)
    assert meta["charged_amount"] == "1000.00"
    assert Decimal(meta["source_charge_reference"]) == Decimal(meta["target_charge_reference"]) == Decimal("1000")
    assert meta["new_start"] == slots[0].start_datetime.isoformat()
    assert meta["previous_input_values"] == {}
    assert not BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.REFUNDED).exists()


@pytest.mark.django_db
def test_second_hop_keeps_same_financial_behaviour(egs_factory, egs_flags_on, egs_quiet_side_effects):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    a, b, c = (egs_factory.equipment(group, unit_charge="1000.00") for _ in range(3))
    owner = egs_factory.student()
    sub = _funded_wallet(owner, egs_factory.department, "1000.00")
    booking = _paid_booking(egs_factory, owner, a, egs_factory.future(days=4, hour=10), amount="1000.00")
    before = _money_snapshot(booking, sub)

    assert _reschedule(egs_factory, owner, booking, b, _free_slots(egs_factory, b, egs_factory.future(5, 11))).status_code == 200
    booking.refresh_from_db()
    res = _reschedule(egs_factory, owner, booking, c, _free_slots(egs_factory, c, egs_factory.future(6, 12)))

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.equipment_id == c.pk
    assert _money_snapshot(booking, sub) == before
    hops = [
        (e.metadata["previous_equipment_id"], e.metadata["new_equipment_id"])
        for e in BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.RESCHEDULED).order_by("pk")
    ]
    assert hops == [(a.pk, b.pk), (b.pk, c.pk)]


@pytest.mark.django_db
def test_full_cancellation_after_reschedule_matches_unmoved_booking(
    egs_factory, egs_flags_on, egs_quiet_side_effects, quiet_cancellation
):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    a = egs_factory.equipment(group, unit_charge="1000.00")
    b = egs_factory.equipment(group, unit_charge="1000.00")
    owner = egs_factory.student()
    sub = _funded_wallet(owner, egs_factory.department, "2000.00")
    moved = _paid_booking(egs_factory, owner, a, egs_factory.future(days=4, hour=10), amount="1000.00")
    control = _paid_booking(egs_factory, owner, a, egs_factory.future(days=4, hour=14), amount="1000.00")
    assert _reschedule(egs_factory, owner, moved, b, _free_slots(egs_factory, b, egs_factory.future(5, 11))).status_code == 200
    moved.refresh_from_db()

    moved_result = _cancel(moved, owner, list(moved.daily_slots.values_list("id", flat=True)))
    control_result = _cancel(control, owner, list(control.daily_slots.values_list("id", flat=True)))

    assert moved_result["is_full_cancel"] and control_result["is_full_cancel"]
    assert moved_result["refund_amount"] == control_result["refund_amount"] == Decimal("1000.00")
    assert moved_result["refund_transaction"].sub_wallet_id == control_result["refund_transaction"].sub_wallet_id == sub.pk
    moved.refresh_from_db()
    control.refresh_from_db()
    assert moved.status == control.status == BookingStatus.REFUNDED
    sub.refresh_from_db()
    assert sub.balance == Decimal("2000.00")


@pytest.mark.django_db
def test_partial_cancellation_after_reschedule_matches_unmoved_booking(
    egs_factory, egs_flags_on, egs_quiet_side_effects, quiet_cancellation
):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    a = egs_factory.equipment(group, unit_charge="1000.00", time_formula="120")
    b = egs_factory.equipment(group, unit_charge="1000.00", time_formula="120")
    owner = egs_factory.student()
    sub = _funded_wallet(owner, egs_factory.department, "4000.00")
    moved = _paid_booking(egs_factory, owner, a, egs_factory.future(days=4, hour=8), amount="2000.00", slot_count=2)
    control = _paid_booking(egs_factory, owner, a, egs_factory.future(days=4, hour=14), amount="2000.00", slot_count=2)
    target_slots = _free_slots(egs_factory, b, egs_factory.future(5, 9), count=2)
    res = _reschedule(egs_factory, owner, moved, b, target_slots)
    assert res.status_code == 200, res.data
    moved.refresh_from_db()
    assert moved.total_charge == Decimal("2000.00")

    results = []
    for booking in (moved, control):
        last_slot = booking.daily_slots.order_by("-start_datetime").first()
        plan = bc.compute_partial_cancel_plan(booking, slot_ids_to_cancel=[last_slot.id])
        results.append((plan, _cancel(booking, owner, [last_slot.id], partial_plan=plan)))

    (moved_plan, moved_result), (control_plan, control_result) = results
    assert moved_plan["new_charge"] == control_plan["new_charge"] == Decimal("1000")
    assert moved_result["refund_amount"] == control_result["refund_amount"] == Decimal("1000.00")
    assert moved_result["refund_transaction"].sub_wallet_id == control_result["refund_transaction"].sub_wallet_id == sub.pk
    moved.refresh_from_db()
    control.refresh_from_db()
    assert moved.total_charge == control.total_charge == Decimal("1000.00")
    assert moved.settlement_department_id == control.settlement_department_id


@pytest.mark.django_db
def test_different_charge_is_rejected_without_adjustment(egs_factory, egs_flags_on, egs_quiet_side_effects):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    a = egs_factory.equipment(group, unit_charge="1000.00")
    pricier = egs_factory.equipment(group, unit_charge="1500.00")
    owner = egs_factory.student()
    sub = _funded_wallet(owner, egs_factory.department, "1000.00")
    booking = _paid_booking(egs_factory, owner, a, egs_factory.future(days=4, hour=10), amount="1000.00")
    before = _money_snapshot(booking, sub)
    slots = _free_slots(egs_factory, pricier, egs_factory.future(5, 11))

    res = _reschedule(egs_factory, owner, booking, pricier, slots)

    assert res.status_code == 400
    assert res.data["code"] == "CHARGE_MISMATCH"
    booking.refresh_from_db()
    assert booking.equipment_id == a.pk
    assert _money_snapshot(booking, sub) == before
    slots[0].refresh_from_db()
    assert slots[0].booking_id is None
    options = egs_factory.client_for(owner).get(f"/api/bookings/{booking.pk}/reschedule-options/")
    assert [o["equipment_id"] for o in options.data["options"]] == [a.pk]


@pytest.mark.django_db
def test_unverifiable_target_charge_is_rejected(egs_factory, egs_flags_on, egs_quiet_side_effects):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    ChargeProfile.objects.filter(equipment=target).update(profile_type=None)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, source, egs_factory.future(days=4, hour=10))

    res = _reschedule(egs_factory, owner, booking, target, _free_slots(egs_factory, target, egs_factory.future(5, 11)))

    assert res.status_code == 400
    assert res.data["code"] == "CHARGE_NOT_VERIFIED"
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk


@pytest.mark.django_db
def test_alternative_uses_target_existing_charge_calculation(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True, alternative_search_other_slots=True)
    source = egs_factory.equipment(group, unit_charge="1000.00")
    target = egs_factory.equipment(group, unit_charge="1000.00")
    owner = egs_factory.student()
    wanted = egs_factory.slot(source, egs_factory.future(days=3, hour=10), status="BOOKED")
    egs_factory.slot(target, wanted.start_datetime)

    results = egs.find_alternatives(
        actor=owner, booking_user=owner, equipment=source, input_values={}, requested_slot_ids=[wanted.id]
    )

    assert [r["equipment_id"] for r in results] == [target.pk]
    cp = egs.resolve_charge_profile(owner, target)
    assert results[0]["estimated_charge"] == egs.estimated_charge_for(target, cp, {}, 60)
    assert Decimal(results[0]["estimated_charge"]) == Decimal("1000")


# ---------------------------------------------------------------------------
# Department rule: defensive reschedule check (corrupted legacy data)
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_same_department_reschedule_passes(egs_factory, egs_flags_on, egs_quiet_side_effects):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, source, egs_factory.future(days=4, hour=10))

    res = _reschedule(egs_factory, owner, booking, target, _free_slots(egs_factory, target, egs_factory.future(5, 11)))

    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.equipment_id == target.pk
    assert target.internal_department_id == source.internal_department_id


@pytest.mark.django_db
def test_corrupted_cross_department_group_reschedule_is_rejected(egs_factory, egs_flags_on, egs_quiet_side_effects):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    source = egs_factory.equipment(group)
    corrupted = egs_factory.equipment(group)
    Equipment.objects.filter(pk=corrupted.pk).update(internal_department=_other_department())
    owner = egs_factory.student()
    sub = _funded_wallet(owner, egs_factory.department, "10.00")
    booking = _paid_booking(egs_factory, owner, source, egs_factory.future(days=4, hour=10), amount="10.00")
    before = _money_snapshot(booking, sub)
    slots = _free_slots(egs_factory, corrupted, egs_factory.future(5, 11))

    with patch.object(egs, "equipment_eligibility_error", return_value=None):
        res = _reschedule(egs_factory, owner, booking, corrupted, slots)

    assert res.status_code == 400
    assert res.data["code"] == "DEPARTMENT_MISMATCH"
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk
    assert _money_snapshot(booking, sub) == before
    slots[0].refresh_from_db()
    assert slots[0].booking_id is None
    assert corrupted.pk not in {m.pk for m in egs.get_group_members(source)}
    options = egs_factory.client_for(owner).get(f"/api/bookings/{booking.pk}/reschedule-options/")
    assert [o["equipment_id"] for o in options.data["options"]] == [source.pk]
    assert Booking.objects.get(pk=booking.pk).settlement_department_id == egs_factory.department.pk
