"""The booking user's "Automatically search and allocate alternate equipment" choice."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from rest_framework.parsers import JSONParser
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory, force_authenticate

from iic_booking.equipment import api_views
from iic_booking.equipment import equipment_group_service as egs


def _drf_post(user, body):
    raw = APIRequestFactory().post("/x/", body, format="json")
    force_authenticate(raw, user=user)
    req = Request(raw, parsers=[JSONParser()])
    req.user = user
    return req


def _alt(target, slot_ids=(42,)):
    return {
        "equipment_id": target.pk, "code": target.code, "name": target.name, "make": "", "model_information": "",
        "slot_ids": list(slot_ids), "input_values": {}, "start": "s", "end": "e",
        "missing_required_fields": [], "input_error": None,
    }


def _impl(source, seen):
    def impl(request, pk):
        seen.append((pk, dict(request.data), getattr(request, "_egs_auto_allocated", False)))
        if pk == source.pk:
            return Response(api_views._enrich_failed_booking_response(
                source, request.user, "taken", waitlist_on_failure=True, slot_unavailable_failure=True
            ), status=400)
        return Response({"booking_id": 7}, status=201)

    return impl


def test_alternative_booking_is_on_by_default_and_controlled_per_group():
    from django.conf import settings

    assert settings.EQUIPMENT_GROUP_ALTERNATIVE_BOOKING_ENABLED is True


@pytest.mark.django_db
def test_ticked_option_auto_allocates_without_group_auto_switch(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    seen = []

    with patch.object(egs, "find_alternatives", return_value=[_alt(target)]):
        res = egs.run_booking_with_group_alternatives(
            _drf_post(user, {"slot_ids": [1], "offer_group_alternatives": True, "auto_allocate_alternative": True}),
            source.pk, _impl(source, seen),
        )

    assert res.status_code == 201
    assert res.data["allocated_alternative"]["equipment"]["equipment_id"] == target.pk
    pk, body, auto = seen[1]
    assert pk == target.pk and auto is True
    assert body["slot_ids"] == [42]
    assert "auto_allocate_alternative" not in body


@pytest.mark.django_db
def test_unticked_option_asks_for_confirmation_even_with_auto_switches_on(egs_factory, egs_flags_on):
    egs_flags_on.EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED = True
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    seen = []

    with patch.object(egs, "find_alternatives", return_value=[_alt(target)]), \
            patch.object(api_views, "add_user_to_waitlist") as wl:
        res = egs.run_booking_with_group_alternatives(
            _drf_post(user, {"slot_ids": [1], "offer_group_alternatives": True, "auto_allocate_alternative": False}),
            source.pk, _impl(source, seen),
        )

    assert res.status_code == 409
    assert res.data["code"] == egs.ALTERNATIVES_AVAILABLE_CODE
    assert [pk for pk, _, _ in seen] == [source.pk]
    wl.assert_not_called()


@pytest.mark.django_db
def test_clients_without_the_option_keep_group_setting(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True, auto_allocation_enabled=True)
    source = egs_factory.equipment(group)

    assert egs.user_wants_auto_allocation({}, source) is False
    egs_flags_on.EQUIPMENT_GROUP_AUTO_ALLOCATION_ENABLED = True
    assert egs.user_wants_auto_allocation({}, source) is True
    assert egs.user_wants_auto_allocation({"auto_allocate_alternative": "false"}, source) is False
    assert egs.user_wants_auto_allocation({"auto_allocate_alternative": "true"}, source) is True


@pytest.mark.django_db
def test_no_slot_selected_request_is_auto_allocated_without_the_no_selection_marker(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    seen = []
    body = {
        "request_waitlist_without_slot_selection": True,
        "waitlist_on_failure": False,
        "offer_group_alternatives": True,
        "auto_allocate_alternative": True,
    }

    with patch.object(egs, "find_alternatives", return_value=[_alt(target, (5, 6))]) as finder:
        res = egs.run_booking_with_group_alternatives(_drf_post(user, body), source.pk, _impl(source, seen))

    assert res.status_code == 201
    assert finder.call_args.kwargs["requested_slot_ids"] == []
    _, target_body, _ = seen[1]
    assert "request_waitlist_without_slot_selection" not in target_body
    assert target_body["slot_ids"] == [5, 6]


@pytest.mark.django_db
def test_without_requested_slots_earliest_slot_is_searched_even_if_group_switch_off(
    egs_factory, egs_flags_on, monkeypatch
):
    monkeypatch.setattr(egs, "_ensure_slots", lambda *a, **k: None)
    group = egs_factory.group(alternative_booking_enabled=True, alternative_search_other_slots=False)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    member = egs_factory.equipment(group)
    start = egs_factory.future(days=3, hour=10)
    egs_factory.slot(source, start, status="BOOKED")
    free = egs_factory.slot(member, start + timedelta(hours=2))

    no_selection = egs.find_alternatives(actor=user, booking_user=user, equipment=source, input_values={})
    assert [(r["equipment_id"], r["slot_ids"], r["exact_match"]) for r in no_selection] == [(member.pk, [free.id], False)]

    requested = egs_factory.slot(source, start + timedelta(days=1), status="BOOKED")
    exact_only = egs.find_alternatives(
        actor=user, booking_user=user, equipment=source, input_values={}, requested_slot_ids=[requested.id]
    )
    assert exact_only == []


@pytest.mark.django_db
@pytest.mark.parametrize("urgent_flag", ["rush_relief", "create_as_hold"])
def test_urgent_bookings_are_not_offered_alternatives(egs_factory, egs_flags_on, urgent_flag):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    seen = []
    body = {"slot_ids": [1], "offer_group_alternatives": True, "auto_allocate_alternative": True, urgent_flag: True}

    with patch.object(egs, "find_alternatives", return_value=[_alt(target)]) as finder, \
            patch.object(api_views, "add_user_to_waitlist", return_value=(True, 1)), \
            patch.object(api_views, "_schedule_unsuccessful_booking_waitlist_email"):
        res = egs.run_booking_with_group_alternatives(_drf_post(user, body), source.pk, _impl(source, seen))

    finder.assert_not_called()
    assert res.status_code == 400
    assert [pk for pk, _, _ in seen] == [source.pk]


@pytest.mark.django_db
def test_no_alternative_found_waitlists_the_user_on_the_original_equipment(egs_factory, egs_flags_on):
    group = egs_factory.group(alternative_booking_enabled=True)
    user = egs_factory.student()
    source = egs_factory.equipment(group)
    seen = []
    body = {"slot_ids": [1], "offer_group_alternatives": True, "auto_allocate_alternative": True,
            "waitlist_on_failure": True}

    with patch.object(egs, "find_alternatives", return_value=[]), \
            patch.object(api_views, "add_user_to_waitlist", return_value=(True, 1)) as wl, \
            patch.object(api_views, "_schedule_unsuccessful_booking_waitlist_email"):
        res = egs.run_booking_with_group_alternatives(_drf_post(user, body), source.pk, _impl(source, seen))

    assert res.status_code == 400
    wl.assert_called_once_with(source, user)
    assert res.data["waitlist_position"] == 1
    assert res.data["error"].startswith("Booking Waitlisted")
