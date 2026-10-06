"""Read-only next slot-window opening (per-equipment rule, else global) for calendar countdowns."""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import InternalUserSlotWindowSetting
from iic_booking.equipment.slot_window_opening import next_slot_window_opening, slot_window_opening_payload

IST = ZoneInfo("Asia/Kolkata")
URL = "/api/slot-window/opening/"


def ist(*parts):
    return datetime(*parts, tzinfo=IST)


@pytest.mark.parametrize(
    "at, expected",
    [
        (ist(2026, 10, 6, 21, 0), ist(2026, 10, 7, 21, 0)),
        (ist(2026, 10, 7, 20, 59, 59), ist(2026, 10, 7, 21, 0)),
        (ist(2026, 10, 7, 21, 0), ist(2026, 10, 14, 21, 0)),
        (ist(2026, 10, 11, 23, 59), ist(2026, 10, 14, 21, 0)),
    ],
)
def test_next_opening_rolls_over_at_the_opening(at, expected):
    assert next_slot_window_opening(2, time(21, 0), at) == expected


def test_no_rule_means_no_opening():
    assert next_slot_window_opening(None, time(21, 0), ist(2026, 10, 6)) is None
    assert next_slot_window_opening(2, None, ist(2026, 10, 6)) is None


@pytest.mark.django_db
def test_equipment_rule_wins_over_global(egs_factory):
    InternalUserSlotWindowSetting.objects.create(reference_weekday=4, reference_time=time(9, 0))
    eq = egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))

    data = slot_window_opening_payload(eq, at=ist(2026, 10, 6, 21, 0))

    assert data["applies"] is True
    assert (data["weekday"], data["time"], data["source"]) == (2, "21:00", "equipment")
    assert datetime.fromisoformat(data["next_opens_at"]) == ist(2026, 10, 7, 21, 0)
    assert data["utc_offset_minutes"] == 330


@pytest.mark.django_db
def test_global_rule_applies_when_equipment_has_none(egs_factory):
    InternalUserSlotWindowSetting.objects.create(reference_weekday=2, reference_time=time(21, 0))
    eq = egs_factory.equipment()

    data = slot_window_opening_payload(eq, at=ist(2026, 10, 7, 21, 30))

    assert (data["applies"], data["source"]) == (True, "global")
    assert datetime.fromisoformat(data["next_opens_at"]) == ist(2026, 10, 14, 21, 0)
    assert slot_window_opening_payload(None, at=ist(2026, 10, 7, 21, 30))["weekday"] == 2


@pytest.mark.django_db
def test_hidden_when_no_rule(egs_factory):
    eq = egs_factory.equipment()

    data = APIClient().get(URL, {"equipment_id": eq.equipment_id}).json()

    assert data["applies"] is False
    assert data["next_opens_at"] is None and data["weekday"] is None
    assert APIClient().get(URL).json()["applies"] is False


@pytest.mark.django_db
def test_public_endpoint_and_visibility(egs_factory):
    eq = egs_factory.equipment(slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0))
    hidden = egs_factory.equipment(
        visible_to_test_accounts_only=True, slot_window_reference_weekday=2, slot_window_reference_time=time(21, 0)
    )
    client = APIClient()

    ok = client.get(URL, {"equipment_id": eq.equipment_id})
    assert ok.status_code == 200
    assert ok.json()["applies"] is True and ok["Cache-Control"] == "no-store"
    assert client.get(URL, {"equipment_id": hidden.equipment_id}).status_code == 404
    assert client.get(URL, {"equipment_id": 987654}).status_code == 404
    assert client.get(URL, {"equipment_id": "abc"}).status_code == 400
