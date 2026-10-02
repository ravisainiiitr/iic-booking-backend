"""Template health: would a saved booking template book cleanly now (checks reuse the booking rules)."""

from __future__ import annotations

from datetime import time
from types import SimpleNamespace

import pytest
from django.core.cache import cache

from iic_booking.equipment.calculators import SAMPLE_SETS_KEY
from iic_booking.equipment.models import (
    BookingInputTemplate,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentGroupQuota,
    QuotaType,
)
from iic_booking.equipment.template_health import check_template, check_values

from .test_booking_template_preferred_slot import _student_with_wallet, no_portal_lock  # noqa: F401

URL = "/api/booking-templates/"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _numeric(eq, key, label, **options):
    return DynamicInputField.objects.create(
        equipment=eq, field_key=key, field_label=label, field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, **options}, default_value="1",
    )


def _equipment(f, **kwargs):
    eq = f.equipment(**kwargs)
    _numeric(eq, "A", "No. of Samples", max=10)
    DynamicInputField.objects.create(
        equipment=eq, field_key="C", field_label="Gas", field_type=DynamicInputFieldType.RADIO,
        options=["N2", "Ar"], default_value="N2",
    )
    return eq


def _codes(health, severity=None):
    return [i["code"] for i in health["issues"] if severity is None or i["severity"] == severity]


def _saved(user, eq, values, **fields):
    return BookingInputTemplate.objects.create(user=user, equipment=eq, name=fields.pop("name", "T"), input_values=values, **fields)


@pytest.mark.django_db
def test_clean_template_is_ok_with_estimate(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student, _sub = _student_with_wallet(egs_factory)
    health = check_template(_saved(student, eq, {"A": "2", "C": "Ar"}))

    assert health["status"] == "ok", health["issues"]
    assert health["required_slots"] == 1
    assert health["booked_minutes"] == 60
    assert health["estimated_charge"] == "10"


@pytest.mark.django_db
def test_values_the_booking_would_reject_or_drop(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    DynamicInputField.objects.create(
        equipment=eq, field_key="D", field_label="Sample type", field_type=DynamicInputFieldType.TEXT, is_required=True,
    )
    student, _sub = _student_with_wallet(egs_factory)
    health = check_template(_saved(student, eq, {"A": "15", "C": "He", "Z": "old"}))

    assert health["status"] == "needs_attention"
    over = next(i for i in health["issues"] if i["code"] == "numeric_max")
    assert (over["field"], over["limit"], over["fix"], over["set"]) == ("A", 10, "clamp", 1)
    assert "max 10 allowed" in over["message"]
    assert "required_missing" in _codes(health, "error")
    assert "option_invalid" in _codes(health, "warning")
    assert "field_removed" in _codes(health, "info")
    assert health["fixable_error_count"] == health["error_count"] == 2


@pytest.mark.django_db
def test_formula_maximum_is_checked_per_sample_set(egs_factory, no_portal_lock):
    eq = egs_factory.equipment(time_formula="B*60")
    _numeric(eq, "A", "No. of Samples", max_formula="B*4")
    _numeric(eq, "B", "Hours")
    student, _sub = _student_with_wallet(egs_factory)
    health = check_template(_saved(student, eq, {"A": "4", "B": "1", SAMPLE_SETS_KEY: [{"A": "9", "B": "2"}]}))

    issue = next(i for i in health["issues"] if i["code"] == "numeric_formula_max")
    assert (issue["set"], issue["limit"], issue["field"]) == (2, 8, "A")
    assert issue["message"].startswith("Sample set 2: ")


@pytest.mark.django_db
def test_sample_sets_dropped_when_equipment_no_longer_allows_them(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory, allow_multiple_sample_sets=False)
    student, _sub = _student_with_wallet(egs_factory)
    health = check_template(_saved(student, eq, {"A": "2", SAMPLE_SETS_KEY: [{"A": "3"}]}))

    assert _codes(health) == ["sample_sets_disabled"]
    assert health["status"] == "advice"


@pytest.mark.django_db
def test_template_needing_more_than_the_weekly_limit(egs_factory, no_portal_lock):
    group = egs_factory.group()
    eq = _equipment(egs_factory, group=group, time_formula="360")
    for quota_type in (QuotaType.WEEKLY, QuotaType.MONTHLY):
        EquipmentGroupQuota.objects.create(
            equipment_group=group, quota_type=quota_type, is_enforced=True,
            internal_individual_quota_minutes=240 if quota_type == QuotaType.WEEKLY else 0,
            internal_faculty_quota_minutes=0, external_individual_quota_minutes=0, external_faculty_quota_minutes=0,
        )
    student, _sub = _student_with_wallet(egs_factory)
    health = check_template(_saved(student, eq, {"A": "1"}))

    issue = next(i for i in health["issues"] if i["code"] == "quota_over_limit")
    assert issue["severity"] == "error"
    assert "needs 6 h" in issue["message"] and "weekly limit on this equipment is 4 h" in issue["message"]


@pytest.mark.django_db
def test_effectively_unlimited_quota_is_ignored(egs_factory, no_portal_lock):
    group = egs_factory.group()
    eq = _equipment(egs_factory, group=group, time_formula="360")
    EquipmentGroupQuota.objects.create(
        equipment_group=group, quota_type=QuotaType.WEEKLY, is_enforced=True,
        internal_individual_quota_minutes=9000, internal_faculty_quota_minutes=9000,
        external_individual_quota_minutes=0, external_faculty_quota_minutes=0,
    )
    student, _sub = _student_with_wallet(egs_factory)
    health = check_template(_saved(student, eq, {"A": "1"}))
    assert not [c for c in _codes(health) if c.startswith("quota")]


@pytest.mark.django_db
def test_wallet_balance_below_estimated_charge_is_advice(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory, unit_charge="500.00")
    student, _sub = _student_with_wallet(egs_factory, balance="100.00")
    health = check_template(_saved(student, eq, {"A": "1"}))

    assert _codes(health) == ["wallet_low"]
    assert health["status"] == "advice"
    assert "₹500)" in health["issues"][0]["message"]


@pytest.mark.django_db
def test_preferred_slot_against_the_slot_schedule(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory, time_formula="180")
    for hour in (9, 10, 11, 12):
        egs_factory.slot(eq, egs_factory.future(days=3, hour=hour))
    student, _sub = _student_with_wallet(egs_factory)

    def preferred(weekday, start):
        return check_values(student, eq, {"A": "1"}, {}, {"weekday": weekday, "start_time": start, "slot_count": 3})

    assert "preferred_weekend" in _codes(preferred(5, time(10, 0)))
    assert "preferred_slot_missing" in _codes(preferred(1, time(10, 30)))
    short = next(i for i in preferred(1, time(11, 0))["issues"] if i["code"] == "preferred_slot_too_short")
    assert (short["needed"], short["available"]) == (3, 2)
    assert not [c for c in _codes(preferred(1, time(9, 0))) if c.startswith("preferred")]


@pytest.mark.django_db
def test_equipment_not_operational_is_not_counted_as_fixable(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory, status="UNDER_MAINTENANCE")
    student, _sub = _student_with_wallet(egs_factory)
    health = check_template(_saved(student, eq, {"A": "1"}))

    assert "equipment_not_operational" in _codes(health, "error")
    assert health["fixable_error_count"] == 0


@pytest.mark.django_db
def test_list_detail_check_and_attention_endpoints(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student, _sub = _student_with_wallet(egs_factory)
    client = egs_factory.client_for(student)
    good = _saved(student, eq, {"A": "2"}, name="Good")
    bad = _saved(student, eq, {"A": "50"}, name="Bad")

    plain = client.get(URL, {"equipment": eq.pk}).data["templates"]
    assert all("health" not in t for t in plain)
    listed = {t["id"]: t for t in client.get(URL, {"equipment": eq.pk, "health": "1"}).data["templates"]}
    assert listed[good.pk]["health"]["status"] == "ok"
    assert listed[bad.pk]["health"]["status"] == "needs_attention"
    assert client.get(f"{URL}{bad.pk}/").data["health"]["issues"][0]["code"] == "numeric_max"

    draft = client.post(
        f"{URL}check/",
        {"equipment": eq.pk, "input_values": {"A": "12"}, "preferred_slot": {"weekday": 6, "start_time": "10:00"}},
        format="json",
    )
    assert draft.status_code == 200, draft.data
    assert {"numeric_max", "preferred_weekend"} <= set(_codes(draft.data))
    assert client.post(f"{URL}check/", {"equipment": 987654}, format="json").status_code == 404

    attention = client.get(f"{URL}attention/").data
    assert attention["total"] == 2 and attention["needs_attention"] == 1
    assert attention["templates"][0]["id"] == bad.pk
    assert attention["templates"][0]["field"] == "A"


@pytest.mark.django_db
def test_fixed_max_is_advice_on_save_and_the_response_says_so(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student, _sub = _student_with_wallet(egs_factory)
    client = egs_factory.client_for(student)

    saved = client.post(URL, {"equipment": eq.pk, "name": "Over", "input_values": {"A": "11"}}, format="json")
    assert saved.status_code == 201, saved.data
    issue = saved.data["health"]["issues"][0]
    assert (issue["code"], issue["limit"], issue["fix"]) == ("numeric_max", 10, "clamp")

    fixed = client.patch(f"{URL}{saved.data['id']}/", {"input_values": {"A": "10"}}, format="json")
    assert fixed.status_code == 200, fixed.data
    assert fixed.data["health"]["status"] == "ok"


@pytest.mark.django_db
def test_peak_window_keeps_only_the_input_checks(egs_factory, no_portal_lock, monkeypatch):
    from iic_booking.equipment import template_health

    eq = _equipment(egs_factory, unit_charge="500.00")
    student, _sub = _student_with_wallet(egs_factory, balance="100.00")
    client = egs_factory.client_for(student)
    template = _saved(student, eq, {"A": "50"})
    monkeypatch.setattr(template_health, "peak_light", lambda: True)

    health = client.get(f"{URL}{template.pk}/").data["health"]
    assert health["light"] is True
    assert _codes(health) == ["numeric_max"]
    assert health["estimated_charge"] is None

    monkeypatch.setattr(template_health, "peak_light", lambda: False)
    full = client.get(f"{URL}{template.pk}/").data["health"]
    assert "light" not in full and "wallet_low" in _codes(full)


@pytest.mark.django_db
def test_booking_records_the_template_it_was_made_with(egs_factory):
    from iic_booking.equipment.api_views import _booking_created_event_metadata

    eq = egs_factory.equipment()
    owner = egs_factory.student()
    template = _saved(owner, eq, {})
    other = egs_factory.student()

    mine = _booking_created_event_metadata(SimpleNamespace(data={"booking_template_id": template.pk}, user=owner), eq)
    assert mine == {"booking_template_id": template.pk}
    assert _booking_created_event_metadata(SimpleNamespace(data={"booking_template_id": template.pk}, user=other), eq) is None
    assert _booking_created_event_metadata(SimpleNamespace(data={"booking_template_id": "x"}, user=owner), eq) is None
