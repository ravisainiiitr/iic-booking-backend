"""Production-readiness audit tests for the Equipment Group enhancement (races, financial safety,
input inheritance, flag gating, permissions, old-code compatibility)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.db import connection
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.request import Request
from rest_framework.parsers import JSONParser

from iic_booking.equipment import api_views
from iic_booking.equipment import equipment_group_service as egs
from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    DailySlot,
    DynamicInputField,
    DynamicInputFieldType,
    Equipment,
    EquipmentGroup,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _drf_post(user, path, body):
    raw = APIRequestFactory().post(path, body, format="json")
    force_authenticate(raw, user=user)
    req = Request(raw, parsers=[JSONParser()])
    req.user = user
    return req


def _cross_setup(egs_factory):
    group = egs_factory.group(cross_rescheduling_enabled=True)
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, source, egs_factory.future(days=4, hour=10))
    target_slot = egs_factory.slot(target, egs_factory.future(days=5, hour=11))
    return source, target, owner, booking, target_slot


def _cross_body(slot, target):
    return {
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "target_equipment_id": target.pk,
    }


def _assert_booking_untouched(booking, source, old_slot_ids, target_slot):
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk
    assert booking.total_charge == Decimal("10.00")
    assert booking.charge_profile.equipment_id == source.pk
    assert set(booking.daily_slots.values_list("id", flat=True)) == set(old_slot_ids)
    target_slot.refresh_from_db()
    assert target_slot.booking_id is None and target_slot.status == "AVAILABLE"


# --- cross-reschedule: financial / transactional safety ------------------------------------


@pytest.mark.django_db
def test_cross_reschedule_target_quota_denied_leaves_booking_unchanged(
    egs_factory, egs_flags_on, egs_quiet_side_effects
):
    source, target, owner, booking, target_slot = _cross_setup(egs_factory)
    old_slot_ids = list(booking.daily_slots.values_list("id", flat=True))

    with patch.object(api_views, "booking_quota_should_skip", return_value=False), patch.object(
        api_views.QuotaService, "validate_booking_quota", return_value=(False, "Quota exceeded on target.")
    ) as quota:
        res = egs_factory.client_for(owner).post(
            f"/api/bookings/{booking.pk}/user-reschedule/", _cross_body(target_slot, target), format="json"
        )

    assert res.status_code == 400
    assert res.data["code"] == "QUOTA_EXCEEDED"
    assert quota.call_args.kwargs["equipment"].pk == target.pk
    assert quota.call_args.kwargs["exclude_booking_id"] == booking.pk
    _assert_booking_untouched(booking, source, old_slot_ids, target_slot)
    assert egs_quiet_side_effects.waitlist == []


@pytest.mark.django_db
def test_cross_reschedule_error_inside_transaction_rolls_back(
    egs_factory, egs_flags_on, egs_quiet_side_effects, monkeypatch
):
    source, target, owner, booking, target_slot = _cross_setup(egs_factory)
    old_slot_ids = list(booking.daily_slots.values_list("id", flat=True))

    def _boom(**kwargs):
        raise RuntimeError("history write failed")

    monkeypatch.setattr(api_views, "create_booking_event", _boom)
    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/", _cross_body(target_slot, target), format="json"
    )

    assert res.status_code == 500
    _assert_booking_untouched(booking, source, old_slot_ids, target_slot)
    assert DailySlot.objects.filter(id__in=old_slot_ids, booking=booking, status="BOOKED").count() == len(old_slot_ids)
    assert egs_quiet_side_effects.waitlist == []


@pytest.mark.django_db
def test_cross_reschedule_booking_cancelled_concurrently_is_not_resurrected(
    egs_factory, egs_flags_on, egs_quiet_side_effects
):
    from iic_booking.equipment.slot_utils import SlotAvailabilityChecker

    source, target, owner, booking, target_slot = _cross_setup(egs_factory)

    def cancel_then_approve(slot):
        Booking.objects.filter(pk=booking.pk).update(status=BookingStatus.CANCELLED)
        return True

    with patch.object(SlotAvailabilityChecker, "is_slot_available", staticmethod(cancel_then_approve)):
        res = egs_factory.client_for(owner).post(
            f"/api/bookings/{booking.pk}/user-reschedule/", _cross_body(target_slot, target), format="json"
        )

    assert res.status_code == 409
    assert res.data["code"] == "BOOKING_CHANGED"
    booking.refresh_from_db()
    assert booking.status == BookingStatus.CANCELLED
    assert booking.equipment_id == source.pk
    target_slot.refresh_from_db()
    assert target_slot.booking_id is None and target_slot.status == "AVAILABLE"


@pytest.mark.django_db
def test_cross_reschedule_flag_off_rejects_even_with_group_switch(egs_factory, egs_flags_off, egs_quiet_side_effects):
    source, target, owner, booking, target_slot = _cross_setup(egs_factory)
    old_slot_ids = list(booking.daily_slots.values_list("id", flat=True))

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/", _cross_body(target_slot, target), format="json"
    )
    assert res.status_code == 400
    assert res.data["code"] == "CROSS_RESCHEDULING_DISABLED"
    _assert_booking_untouched(booking, source, old_slot_ids, target_slot)


# --- input inheritance ------------------------------------------------------------------------


def _field(eq, key, label, ftype=DynamicInputFieldType.NUMERIC, required=False, options=None, link=None):
    return DynamicInputField.objects.create(
        equipment=eq, field_key=key, field_label=label, field_type=ftype, is_required=required,
        options=options, source_element_field_key=link,
    )


@pytest.mark.django_db
def test_inputs_identical_definitions_copy_everything(egs_factory):
    group = egs_factory.group()
    src, tgt = egs_factory.equipment(group), egs_factory.equipment(group)
    for eq in (src, tgt):
        _field(eq, "A", "Number of samples", required=True)
        _field(eq, "B", "Mode", DynamicInputFieldType.RADIO, options=["Fast", "Slow"])
    mapping = egs.map_inputs(src, tgt, "student", {"A": "2", "B": "Fast"})
    assert mapping.values == {"A": "2", "B": "Fast"}
    assert mapping.dropped == [] and mapping.complete


@pytest.mark.django_db
def test_inputs_target_extra_required_field_is_reported_not_guessed(egs_factory):
    group = egs_factory.group()
    src, tgt = egs_factory.equipment(group), egs_factory.equipment(group)
    _field(src, "A", "Number of samples")
    _field(tgt, "A", "Number of samples")
    _field(tgt, "B", "Detector", DynamicInputFieldType.TEXT, required=True)
    _field(tgt, "C", "Notes", DynamicInputFieldType.TEXT)
    mapping = egs.map_inputs(src, tgt, "student", {"A": "2"})
    assert mapping.values == {"A": "2"}
    assert [m["key"] for m in mapping.missing_required] == ["B"]
    assert mapping.complete is False


@pytest.mark.django_db
def test_inputs_same_key_different_type_or_label_is_dropped(egs_factory):
    group = egs_factory.group()
    src, tgt = egs_factory.equipment(group), egs_factory.equipment(group)
    _field(src, "A", "Number of samples")
    _field(src, "B", "Temperature")
    _field(tgt, "A", "Number of samples", DynamicInputFieldType.TEXT)
    _field(tgt, "B", "Pressure")
    mapping = egs.map_inputs(src, tgt, "student", {"A": "2", "B": "300"})
    assert mapping.values == {}
    assert sorted(d["key"] for d in mapping.dropped) == ["A", "B"]


@pytest.mark.django_db
def test_inputs_structured_fields_need_identical_definition(egs_factory):
    group = egs_factory.group()
    src, same, different, moved = (egs_factory.equipment(group) for _ in range(4))
    columns = [{"label": "Element"}, {"label": "Conc."}]
    table = [["Fe", "1"]]
    _field(src, "C", "Samples table", DynamicInputFieldType.TABLE, options=columns, link="A")
    _field(same, "C", "Samples table", DynamicInputFieldType.TABLE, options=columns, link="A")
    _field(different, "C", "Samples table", DynamicInputFieldType.TABLE, options=[{"label": "Element"}], link="A")
    _field(moved, "D", "Samples table", DynamicInputFieldType.TABLE, options=columns, link="A")

    assert egs.map_inputs(src, same, "student", {"C": table}).values == {"C": table}
    for tgt in (different, moved):
        mapping = egs.map_inputs(src, tgt, "student", {"C": table})
        assert mapping.values == {}
        assert [d["key"] for d in mapping.dropped] == ["C"]


@pytest.mark.django_db
def test_inputs_companion_keys_follow_their_base_field(egs_factory):
    group = egs_factory.group()
    src, tgt = egs_factory.equipment(group), egs_factory.equipment(group)
    _field(src, "B", "Sample type", DynamicInputFieldType.RADIO, options=["Powder", "Other"])
    _field(tgt, "B", "Sample type", DynamicInputFieldType.RADIO, options=["Powder"])

    dropped = egs.map_inputs(src, tgt, "student", {"B": "Other", "B_other": "gel"})
    assert dropped.values == {}
    kept = egs.map_inputs(src, tgt, "student", {"B": "Powder", "B_other": ""})
    assert kept.values == {"B": "Powder", "B_other": ""}


# --- discovery: visibility and per-equipment slot window ------------------------------------------


@pytest.mark.django_db
def test_hidden_equipment_is_never_offered(egs_factory, monkeypatch):
    monkeypatch.setattr(egs, "_ensure_slots", lambda *a, **k: None)
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    visible = egs_factory.equipment(group)
    hidden = egs_factory.equipment(group)
    start = egs_factory.future(days=3, hour=10)
    requested = egs_factory.slot(source, start, status="BOOKED")
    egs_factory.slot(visible, start)
    egs_factory.slot(hidden, start)

    real = api_views.user_can_see_equipment
    monkeypatch.setattr(api_views, "user_can_see_equipment", lambda u, eq: eq.pk != hidden.pk and real(u, eq))
    results = egs.find_alternatives(
        actor=user, booking_user=user, equipment=source, input_values={}, requested_slot_ids=[requested.id]
    )
    assert [r["equipment_id"] for r in results] == [visible.pk]


@pytest.mark.django_db
def test_exact_slot_outside_member_window_is_not_offered(egs_factory, monkeypatch):
    monkeypatch.setattr(egs, "_ensure_slots", lambda *a, **k: None)
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    member = egs_factory.equipment(group)
    start = egs_factory.future(days=3, hour=10)
    requested = egs_factory.slot(source, start, status="BOOKED")
    egs_factory.slot(member, start)

    today = timezone.localdate()
    real_bounds = egs.slot_window_bounds

    def bounds(eq, user_type, is_admin):
        if eq.pk == member.pk:
            return today + timedelta(days=10), today + timedelta(days=12)
        return real_bounds(eq, user_type, is_admin)

    monkeypatch.setattr(egs, "slot_window_bounds", bounds)
    results = egs.find_alternatives(
        actor=user, booking_user=user, equipment=source, input_values={}, requested_slot_ids=[requested.id]
    )
    assert results == []


# --- booking wrapper -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_flags_off_wrapper_is_a_direct_call_without_queries(egs_factory, egs_flags_off, django_assert_num_queries):
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    source = egs_factory.equipment(group)
    user = egs_factory.student()
    sentinel = Response({"ok": True}, status=201)
    req = _drf_post(user, "/x/", {"slot_ids": [1], "offer_group_alternatives": True})

    with django_assert_num_queries(0):
        res = egs.run_booking_with_group_alternatives(req, source.pk, lambda r, pk: sentinel)
    assert res is sentinel


@pytest.mark.django_db
def test_successful_booking_is_returned_even_if_deferral_was_recorded(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    booked = Response({"booking_id": 1}, status=201)

    def impl(request, pk):
        api_views._enrich_failed_booking_response(
            source, request.user, "first attempt taken", waitlist_on_failure=True, slot_unavailable_failure=True
        )
        return booked

    with patch.object(egs, "find_alternatives") as finder:
        res = egs.run_booking_with_group_alternatives(
            _drf_post(user, "/x/", {"slot_ids": [1], "offer_group_alternatives": True}), source.pk, impl
        )
    assert res is booked
    finder.assert_not_called()


@pytest.mark.django_db
def test_auto_allocation_off_requires_user_confirmation(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    calls = []
    alt = {"equipment_id": 999, "missing_required_fields": [], "input_error": None}

    def impl(request, pk):
        calls.append(pk)
        return Response(api_views._enrich_failed_booking_response(
            source, request.user, "taken", waitlist_on_failure=True, slot_unavailable_failure=True
        ), status=400)

    with patch.object(egs, "find_alternatives", return_value=[alt]), patch.object(api_views, "add_user_to_waitlist") as wl:
        res = egs.run_booking_with_group_alternatives(
            _drf_post(user, "/x/", {"slot_ids": [1], "offer_group_alternatives": True}), source.pk, impl
        )
    assert res.status_code == 409
    assert calls == [source.pk]
    wl.assert_not_called()


@pytest.mark.django_db
def test_auto_allocation_race_on_target_does_not_waitlist_there(egs_factory, egs_flags_on):
    egs_flags_on.EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED = True
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    alt = {
        "equipment_id": target.pk, "code": target.code, "name": target.name, "make": "", "model_information": "",
        "slot_ids": [42], "input_values": {}, "start": "s", "end": "e",
        "missing_required_fields": [], "input_error": None,
    }
    waitlisted = []

    def impl(request, pk):
        eq = source if pk == source.pk else target
        return Response(api_views._enrich_failed_booking_response(
            eq, request.user, "taken", waitlist_on_failure=request.data.get("waitlist_on_failure", True),
            slot_unavailable_failure=True,
        ), status=400)

    def add_to_waitlist(equipment, booking_user):
        waitlisted.append(equipment.pk)
        return True, 1

    with patch.object(egs, "find_alternatives", return_value=[alt]), \
            patch.object(api_views, "add_user_to_waitlist", side_effect=add_to_waitlist), \
            patch.object(api_views, "_schedule_unsuccessful_booking_waitlist_email"), \
            patch.object(api_views, "is_slot_window_peak_waitlist_period", side_effect=lambda eq: eq.pk == target.pk):
        res = egs.run_booking_with_group_alternatives(
            _drf_post(user, "/x/", {"slot_ids": [1], "offer_group_alternatives": True}), source.pk, impl
        )

    assert res.status_code == 400
    assert waitlisted == [source.pk]
    assert res.data.get("waitlist_position") == 1


# --- permissions ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_main_admin_can_toggle_group_switches(egs_factory):
    group = egs_factory.group()
    url = f"/api/admin/equipment-groups/{group.pk}/"
    unscoped_staff = UserFactory(user_type=UserType.EXTERNAL_RELATIONS)
    main_admin = UserFactory(user_type=UserType.ADMIN, is_staff=True)

    with patch("iic_booking.users.rbac.user_has_admin_panel_access", return_value=True):
        egs_factory.client_for(unscoped_staff).patch(url, {"alternative_booking_enabled": True}, format="json")
        group.refresh_from_db()
        assert group.alternative_booking_enabled is False

        res = egs_factory.client_for(main_admin).patch(url, {"alternative_booking_enabled": True}, format="json")
        assert res.status_code == 200, getattr(res, "data", None)
        group.refresh_from_db()
        assert group.alternative_booking_enabled is True


# --- old code on the new schema ------------------------------------------------------------------------


@pytest.mark.django_db
def test_new_columns_have_database_defaults_for_old_code():
    now = timezone.now()
    table = EquipmentGroup._meta.db_table
    with connection.cursor() as cursor:
        cursor.execute(
            f"INSERT INTO {table} (name, code, created_at, updated_at) VALUES (%s, %s, %s, %s)",
            ["Legacy insert", "EGS-LEGACY", now, now],
        )
    group = EquipmentGroup.objects.get(code="EGS-LEGACY")
    assert group.alternative_booking_enabled is False
    assert group.alternative_search_other_slots is False
    assert group.auto_allocation_enabled is False
    assert group.cross_rescheduling_enabled is False
    db_default = Equipment._meta.get_field("alternative_priority").db_default
    assert getattr(db_default, "value", db_default) == 100
