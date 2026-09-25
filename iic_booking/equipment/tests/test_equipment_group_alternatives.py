"""Equipment Group alternatives during new booking (flags, eligibility, input mapping, discovery, deferral)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rest_framework import status
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate
from rest_framework.request import Request
from rest_framework.parsers import JSONParser

from iic_booking.equipment import equipment_group_service as egs
from iic_booking.equipment.models import DynamicInputField, DynamicInputFieldType


def _drf_post(user, path, body):
    factory = APIRequestFactory()
    raw = factory.post(path, body, format="json")
    force_authenticate(raw, user=user)
    req = Request(raw, parsers=[JSONParser()])
    req.user = user
    return req


# --- flags -----------------------------------------------------------------


@pytest.mark.django_db
def test_alternative_flag_needs_env_and_group_switch(egs_factory, settings):
    group_on = egs_factory.group(alternative_booking_enabled=True)
    group_off = egs_factory.group(alternative_booking_enabled=False)
    eq_on = egs_factory.equipment(group_on)
    eq_off = egs_factory.equipment(group_off)
    eq_none = egs_factory.equipment(None)

    settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED = False
    assert egs.alternative_booking_enabled(eq_on) is False

    settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED = True
    assert egs.alternative_booking_enabled(eq_on) is True
    assert egs.alternative_booking_enabled(eq_off) is False
    assert egs.alternative_booking_enabled(eq_none) is False


@pytest.mark.django_db
def test_auto_allocation_requires_alternatives_flag(egs_factory, settings):
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    eq = egs_factory.equipment(group)
    settings.EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED = True
    settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED = False
    assert egs.auto_allocation_enabled(eq) is False
    settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED = True
    assert egs.auto_allocation_enabled(eq) is True


# --- membership / eligibility ------------------------------------------------


@pytest.mark.django_db
def test_group_members_ordered_by_priority_then_id_and_scoped_to_group(egs_factory):
    group = egs_factory.group()
    other_group = egs_factory.group()
    source = egs_factory.equipment(group, priority=1)
    low = egs_factory.equipment(group, priority=50)
    high_a = egs_factory.equipment(group, priority=10)
    high_b = egs_factory.equipment(group, priority=10)
    egs_factory.equipment(other_group, priority=1)

    members = list(egs.get_group_members(source))
    assert [m.pk for m in members] == [high_a.pk, high_b.pk, low.pk]


@pytest.mark.django_db
def test_eligibility_rejects_inactive_and_missing_charge_profile(egs_factory):
    group = egs_factory.group()
    user = egs_factory.student()
    ok = egs_factory.equipment(group)
    inactive = egs_factory.equipment(group, status="REPAIR")
    no_profile = egs_factory.equipment(group, with_profile=False)

    assert egs.equipment_eligibility_error(user, ok) is None
    assert "not operational" in egs.equipment_eligibility_error(user, inactive).lower()
    assert "charge profile" in egs.equipment_eligibility_error(user, no_profile).lower()


# --- input mapping -------------------------------------------------------------


def _field(eq, key, label, ftype=DynamicInputFieldType.NUMERIC, required=False, options=None):
    return DynamicInputField.objects.create(
        equipment=eq,
        field_key=key,
        field_label=label,
        field_type=ftype,
        is_required=required,
        options=options,
    )


@pytest.mark.django_db
def test_map_inputs_copies_only_compatible_fields(egs_factory):
    group = egs_factory.group()
    src = egs_factory.equipment(group)
    tgt = egs_factory.equipment(group)
    _field(src, "A", "Number of samples")
    _field(src, "B", "Sample type", DynamicInputFieldType.RADIO, options=["Powder", "Film"])
    _field(src, "C", "Only on source")
    _field(tgt, "A", "Number of samples")
    _field(tgt, "D", "Sample type", DynamicInputFieldType.RADIO, options=["Powder"])
    _field(tgt, "E", "Coating", DynamicInputFieldType.TEXT, required=True)

    mapping = egs.map_inputs(src, tgt, "student", {"A": "3", "B": "Powder", "C": "9"})
    assert mapping.values == {"A": "3", "D": "Powder"}
    assert [d["key"] for d in mapping.dropped] == ["C"]
    assert [m["key"] for m in mapping.missing_required] == ["E"]
    assert mapping.complete is False

    invalid_option = egs.map_inputs(src, tgt, "student", {"A": "1", "B": "Film"})
    assert "D" not in invalid_option.values
    assert [d["key"] for d in invalid_option.dropped] == ["B"]


# --- discovery -------------------------------------------------------------------


@pytest.mark.django_db
def test_find_alternatives_prefers_exact_slot_and_skips_other_groups(egs_factory, monkeypatch):
    monkeypatch.setattr(egs, "_ensure_slots", lambda *a, **k: None)
    group = egs_factory.group(alternative_booking_enabled=True, alternative_search_other_slots=True)
    other_group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    exact_member = egs_factory.equipment(group, priority=90)
    earlier_member = egs_factory.equipment(group, priority=1)
    outsider = egs_factory.equipment(other_group)

    start = egs_factory.future(days=3, hour=10)
    requested = egs_factory.slot(source, start, status="BOOKED")
    exact = egs_factory.slot(exact_member, start)
    egs_factory.slot(earlier_member, start - timedelta(days=1))
    egs_factory.slot(outsider, start)

    results = egs.find_alternatives(
        actor=user, booking_user=user, equipment=source, input_values={}, requested_slot_ids=[requested.id]
    )
    ids = [r["equipment_id"] for r in results]
    assert outsider.pk not in ids
    assert ids[0] == exact_member.pk and results[0]["exact_match"] is True
    assert results[0]["slot_ids"] == [exact.id]
    assert earlier_member.pk in ids and results[1]["exact_match"] is False


@pytest.mark.django_db
def test_find_alternatives_is_read_only(egs_factory, monkeypatch):
    from iic_booking.equipment.models import Booking, DailySlot

    monkeypatch.setattr(egs, "_ensure_slots", lambda *a, **k: None)
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    member = egs_factory.equipment(group)
    start = egs_factory.future()
    requested = egs_factory.slot(source, start, status="BOOKED")
    slot = egs_factory.slot(member, start)

    before = Booking.objects.count()
    egs.find_alternatives(actor=user, booking_user=user, equipment=source, input_values={},
                          requested_slot_ids=[requested.id])
    assert Booking.objects.count() == before
    slot.refresh_from_db()
    assert slot.status == "AVAILABLE" and slot.booking_id is None
    assert DailySlot.objects.get(pk=slot.pk).booking_id is None


# --- deferral in book_equipment -------------------------------------------------------


def _impl_that_fails_on_slot(equipment):
    """Stand-in for _book_equipment_impl: exercise the real deferral hook, then return 400."""
    from iic_booking.equipment.api_views import _enrich_failed_booking_response

    calls = []

    def impl(request, pk):
        calls.append((request, pk))
        payload = _enrich_failed_booking_response(
            equipment, request.user, "Slot unavailable", waitlist_on_failure=True, slot_unavailable_failure=True
        )
        return Response(payload, status=status.HTTP_400_BAD_REQUEST)

    return impl, calls


@pytest.mark.django_db
def test_opted_in_failure_returns_409_with_alternatives_and_skips_waitlist(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    impl, calls = _impl_that_fails_on_slot(source)
    fake_alt = {"equipment_id": 999, "missing_required_fields": [], "input_error": None}

    with patch("iic_booking.equipment.api_views.add_user_to_waitlist") as add_wl, patch.object(
        egs, "find_alternatives", return_value=[fake_alt]
    ):
        req = _drf_post(user, f"/api/equipments/{source.pk}/book/",
                        {"slot_ids": [1], "offer_group_alternatives": True})
        res = egs.run_booking_with_group_alternatives(req, source.pk, impl)

    assert res.status_code == 409
    assert res.data["code"] == egs.ALTERNATIVES_AVAILABLE_CODE
    assert res.data["alternatives"] == [fake_alt]
    assert res.data["original_equipment"]["equipment_id"] == source.pk
    add_wl.assert_not_called()
    assert len(calls) == 1


@pytest.mark.django_db
def test_no_alternatives_falls_back_to_existing_waitlist(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    impl, _ = _impl_that_fails_on_slot(source)

    with patch("iic_booking.equipment.api_views._enrich_failed_booking_response",
               return_value={"error": "Booking waitlisted", "waitlist_position": 1}) as enrich, \
            patch.object(egs, "find_alternatives", return_value=[]):
        req = _drf_post(user, f"/api/equipments/{source.pk}/book/",
                        {"slot_ids": [1], "offer_group_alternatives": True})
        res = egs.run_booking_with_group_alternatives(req, source.pk, impl)

    assert res.status_code == 400
    assert res.data["waitlist_position"] == 1
    assert enrich.call_args.kwargs["slot_unavailable_failure"] is True


@pytest.mark.django_db
def test_skip_param_and_peak_period_keep_existing_behaviour(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    impl, calls = _impl_that_fails_on_slot(source)

    with patch("iic_booking.equipment.api_views.add_user_to_waitlist", return_value=(True, 1)) as add_wl, \
            patch.object(egs, "find_alternatives") as finder:
        req = _drf_post(user, "/x/", {"offer_group_alternatives": True, "skip_group_alternatives": True})
        res = egs.run_booking_with_group_alternatives(req, source.pk, impl)
        assert res.status_code == 400
        finder.assert_not_called()
        assert add_wl.call_count == 1

        with patch("iic_booking.equipment.api_views.is_slot_window_peak_waitlist_period", return_value=True):
            req = _drf_post(user, "/x/", {"offer_group_alternatives": True})
            egs.run_booking_with_group_alternatives(req, source.pk, impl)
        finder.assert_not_called()
    assert len(calls) == 2


@pytest.mark.django_db
def test_auto_allocation_books_first_viable_alternative(egs_factory, egs_flags_on):
    egs_flags_on.EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED = True
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    alt = {
        "equipment_id": target.pk, "code": target.code, "name": target.name, "make": "", "model_information": "",
        "slot_ids": [42], "input_values": {"A": "1"}, "start": "s", "end": "e",
        "missing_required_fields": [], "input_error": None,
    }
    seen = []

    def impl(request, pk):
        seen.append((pk, dict(request.data), getattr(request, "_egs_auto_allocated", False)))
        if pk == source.pk:
            from iic_booking.equipment.api_views import _enrich_failed_booking_response

            return Response(_enrich_failed_booking_response(
                source, request.user, "taken", waitlist_on_failure=True, slot_unavailable_failure=True
            ), status=400)
        return Response({"booking_id": 7}, status=201)

    with patch.object(egs, "find_alternatives", return_value=[alt]):
        req = _drf_post(user, "/x/", {"slot_ids": [1], "offer_group_alternatives": True, "input_values": {"A": "1"}})
        res = egs.run_booking_with_group_alternatives(req, source.pk, impl)

    assert res.status_code == 201
    assert res.data["allocated_alternative"]["equipment"]["equipment_id"] == target.pk
    pk, body, auto = seen[1]
    assert pk == target.pk and auto is True
    assert body["slot_ids"] == [42]
    assert body["alternative_of_equipment_id"] == source.pk
    assert body["waitlist_on_failure"] is False
    assert "offer_group_alternatives" not in body


# --- audit ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_created_event_metadata_records_valid_source_only(egs_factory, egs_flags_on):
    from iic_booking.equipment.api_views import _booking_created_event_metadata

    group = egs_factory.group(alternative_booking_enabled=True)
    other = egs_factory.group(alternative_booking_enabled=True)
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    stranger = egs_factory.equipment(other)

    ok = _booking_created_event_metadata(SimpleNamespace(data={"alternative_of_equipment_id": source.pk}), target)
    assert ok["equipment_group_alternative"] is True
    assert ok["alternative_of_equipment_id"] == source.pk
    assert ok["auto_allocated"] is False

    forged = _booking_created_event_metadata(SimpleNamespace(data={"alternative_of_equipment_id": stranger.pk}), target)
    assert forged is None
    assert _booking_created_event_metadata(SimpleNamespace(data={}), target, atmosphere_sensitive_sample=True) == {
        "atmosphere_sensitive_sample": True
    }
