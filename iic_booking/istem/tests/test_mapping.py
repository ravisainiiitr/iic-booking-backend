"""Payload building: the 19 mandatory Create Booking fields come only from local data or verified mappings, and
anything that cannot be established is reported instead of being substituted."""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from iic_booking.istem import contract
from iic_booking.istem.choices import CodeType
from iic_booking.istem.mapping import (
    build_confirm_payload,
    build_create_booking_payload,
    build_public_user_payload,
    resolve_code,
    resolve_equipment_mapping,
)
from iic_booking.istem.models import IstemBookingLink, IstemCodeMapping, IstemUserLink

from .support import link_user, make_booking, map_codes, map_equipment, ready_booking

pytestmark = pytest.mark.django_db

# [SPEC] Create/Confirm Booking examples: "2025-10-18 14:00:00" (the older SOP's ".000" suffix is not used).
DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def _payload(booking):
    return build_create_booking_payload(booking, IstemUserLink.objects.filter(user=booking.user).first())


def test_complete_booking_produces_the_mandatory_and_staging_descriptive_fields():
    booking = ready_booking()
    result = _payload(booking)
    assert result.ok, result.errors
    assert set(result.payload) == set(contract.CREATE_BOOKING_MANDATORY_FIELDS) | set(contract.CREATE_BOOKING_DESCRIPTIVE_FIELDS)
    p = result.payload
    assert p["equipment_id"] == "12206"
    assert p["public_user_id"] == "35151"
    assert DATETIME.match(p["preferred_timings_from"]) and DATETIME.match(p["preferred_timings_to"])
    assert DATETIME.match(p["request_date"])
    assert p["service_type"] == "Characterization"
    assert p["usage_type"] == p["user_type"] == "Industry User"
    assert p["equipment_uom"] == "Hour"
    assert p["analysis_type"] == "analysis1"
    assert (p["total_est_amt"], p["eq_rate"], p["booking_no_sample"], p["working_hrs"]) == ("1500", "500", "3", "3")
    assert p["billing_user_name"] == "Asha Labs"
    assert p["billing_state"] == "Uttarakhand"
    assert p["booking_gstn"] == "09AAAAA0000A1Z5"
    assert "Roorkee" in p["billing_address"] and "247667" in p["billing_address"]
    assert p["booking_title"].startswith(booking.virtual_booking_id)
    assert all(isinstance(v, str) for v in p.values())  # all form fields are strings
    # [STAGING] booking_remark is required in practice, and the descriptive fields are sent with neutral values.
    assert p["booking_remark"] == f"IIC booking {booking.virtual_booking_id}"
    assert (p["req_unit"], p["gstin"], p["sample_rows"], p["addl_amt"], p["exclusive_use"]) == (
        "3:00", "09AAAAA0000A1Z5", "[]", "0", "No")
    assert p["consumables_provided by"] == p["consumables_provided_by"] == "Institution Facility"
    assert p["bk_natsamp"] == p["specific_information"] == "NA"


def test_booking_times_are_formatted_in_ist_whatever_the_server_time_zone(settings):
    from datetime import datetime, timezone as dt_timezone

    from iic_booking.istem.mapping import format_booking_datetime, format_execute_date

    settings.TIME_ZONE = "UTC"
    moment = datetime(2026, 10, 19, 20, 0, 0, tzinfo=dt_timezone.utc)
    assert format_booking_datetime(moment) == "2026-10-20 01:30:00"
    assert format_execute_date(moment) == "2026-10-20"


def test_unmapped_equipment_blocks_with_actionable_reason():
    booking = make_booking()
    link_user(booking.user)
    result = _payload(booking)
    assert "equipment_id" in result.errors
    assert "no I-STEM equipment mapping" in result.errors["equipment_id"]


def test_unverified_or_inactive_or_non_numeric_equipment_mapping_is_rejected():
    booking = make_booking()
    mapping = map_equipment(booking.equipment, verified=False)
    assert resolve_equipment_mapping(booking.equipment)[1] is not None
    mapping.verified_at, mapping.is_active = mapping.created_at, False
    mapping.save()
    assert "inactive" in resolve_equipment_mapping(booking.equipment)[1]
    mapping.is_active, mapping.istem_equipment_id = True, "EQ-12"
    mapping.save()
    assert "not a valid numeric" in resolve_equipment_mapping(booking.equipment)[1]


def test_user_without_istem_link_is_reported():
    booking = make_booking()
    map_equipment(booking.equipment)
    map_codes(booking.equipment)
    result = _payload(booking)
    assert list(result.errors) == ["public_user_id"]


def test_missing_code_mapping_is_not_defaulted():
    booking = ready_booking()
    IstemCodeMapping.objects.filter(code_type=CodeType.SERVICE_TYPE).delete()
    result = _payload(booking)
    assert "service_type" in result.errors
    assert "service_type" not in result.payload


def test_code_mapping_with_a_value_outside_the_istem_list_is_rejected():
    booking = ready_booking()
    IstemCodeMapping.objects.filter(code_type=CodeType.USAGE_TYPE).update(istem_value="Industry")
    assert "not an accepted I-STEM value" in _payload(booking).errors["usage_type"]


def test_code_resolution_prefers_equipment_override_then_global_key_then_global_wildcard():
    booking = make_booking()
    eq = booking.equipment
    IstemCodeMapping.objects.create(code_type=CodeType.SERVICE_TYPE, local_key="*", istem_value="Packaging")
    assert resolve_code(CodeType.SERVICE_TYPE, "HOUR", equipment=eq)[0] == "Packaging"
    IstemCodeMapping.objects.create(code_type=CodeType.SERVICE_TYPE, local_key="HOUR", istem_value="Fabrication")
    assert resolve_code(CodeType.SERVICE_TYPE, "HOUR", equipment=eq)[0] == "Fabrication"
    IstemCodeMapping.objects.create(code_type=CodeType.SERVICE_TYPE, local_key="HOUR", istem_value="Characterization", equipment=eq)
    assert resolve_code(CodeType.SERVICE_TYPE, "HOUR", equipment=eq)[0] == "Characterization"


def test_analysis_type_requires_an_equipment_specific_mapping():
    booking = ready_booking()
    IstemCodeMapping.objects.filter(code_type=CodeType.ANALYSIS_TYPE).delete()
    IstemCodeMapping.objects.create(code_type=CodeType.ANALYSIS_TYPE, local_key="*", istem_value="global")
    assert "equipment-specific" in _payload(booking).errors["analysis_type"]


@pytest.mark.parametrize("field,kwargs", [
    ("total_est_amt", {"total": Decimal("1500.50")}),
    ("eq_rate", {"rate": Decimal("499.99")}),
    ("booking_no_sample", {"samples": ""}),
    ("booking_no_sample", {"samples": "2.5"}),
    ("booking_gstn", {"gstin": ""}),
    ("booking_gstn", {"gstin": "SHORT"}),
])
def test_values_that_would_need_inventing_or_rounding_are_blocked(field, kwargs):
    booking = ready_booking(**kwargs)
    result = _payload(booking)
    assert field in result.errors
    assert field not in result.payload


def test_charge_profile_without_a_single_rate_blocks_eq_rate():
    booking = ready_booking(profile_type="MULTI_PARAM")
    assert "eq_rate" in _payload(booking).errors


def test_missing_billing_profile_blocks_billing_fields():
    booking = ready_booking(with_billing=False)
    errors = _payload(booking).errors
    assert {"billing_user_name", "billing_address", "billing_state", "booking_gstn"} <= set(errors)


def test_billing_name_must_be_alphabetic():
    booking = ready_booking()
    booking.user.external_billing_profile.billing_name = "Asha Labs Pvt. Ltd."
    booking.user.external_billing_profile.save()
    booking.user.refresh_from_db()
    assert "billing_user_name" in _payload(booking).errors


# --- Create User -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("institute_id", [None, "", "abc"])
def test_create_user_payload_is_blocked_without_institute_id_or_unavailable_profile_fields(institute_id):
    booking = make_booking()
    result = build_public_user_payload(booking.user, institute_id)
    for field in ("institute_id", "user_organisation", "user_salutation", "user_district", "user_type"):
        assert field in result.errors
    assert "institute_id" not in result.payload


def test_create_user_type_uses_its_own_code_list_and_the_token_institute():
    booking = make_booking()
    IstemCodeMapping.objects.create(code_type=CodeType.PUBLIC_USER_TYPE, local_key=booking.user.user_type, istem_value="Industry User")
    result = build_public_user_payload(booking.user, "213")
    assert result.payload["user_type"] == "Industry User"
    assert result.payload["institute_id"] == "213"
    IstemCodeMapping.objects.filter(code_type=CodeType.PUBLIC_USER_TYPE).update(istem_value="Start-ups Company")
    assert "user_type" in build_public_user_payload(booking.user, "213").errors


# --- Confirm Booking ---------------------------------------------------------------------------------------------

def _created_link(booking):
    from iic_booking.istem.redaction import redact

    return IstemBookingLink.objects.create(booking=booking, create_payload=redact(_payload(booking).payload))


def test_confirm_payload_needs_an_approver_request_id_and_approval_time(settings):
    from django.utils import timezone

    settings.ISTEM_CONFIRM_APPROVED_BY = ""
    booking = ready_booking()
    link = _created_link(booking)
    result = build_confirm_payload(booking, link, approved_on=None)
    assert {"approved_by", "request_id", "approved_on"} <= set(result.errors) and result.payload == {}

    settings.ISTEM_CONFIRM_APPROVED_BY = "OP_IITR"
    link.request_id = "55401"
    result = build_confirm_payload(booking, link, approved_on=timezone.now())
    assert result.ok, result.errors
    # Without the booking payload only the five required fields are built.
    assert set(result.payload) == set(contract.CONFIRM_MANDATORY_FIELDS)
    assert result.payload["request_id"] == "55401" and result.payload["approved_by"] == "OP_IITR"
    sent = _payload(booking).payload
    assert result.payload["allocated_timings_from"] == sent["preferred_timings_from"]
    assert result.payload["allocated_timings_to"] == sent["preferred_timings_to"]
    assert DATETIME.match(result.payload["approved_on"])

    # [STAGING] With it, the full booking is re-sent (unmasked) plus booking_status "Slot Allocated".
    full = build_confirm_payload(booking, link, approved_on=timezone.now(), booking_payload=sent).payload
    assert set(contract.CONFIRM_MANDATORY_FIELDS) <= set(full) and set(sent) <= set(full)
    assert full["booking_status"] == "Slot Allocated"
    assert full["billing_user_name"] == "Asha Labs" and full["booking_gstn"] == "09AAAAA0000A1Z5"
    assert full["request_id"] == "55401"


def test_confirm_payload_refuses_a_window_different_from_the_one_requested(settings):
    from datetime import timedelta

    from django.db.models import F
    from django.utils import timezone

    settings.ISTEM_CONFIRM_APPROVED_BY = "OP_IITR"
    booking = ready_booking()
    link = _created_link(booking)
    link.request_id = "55401"
    booking.daily_slots.update(start_datetime=F("start_datetime") + timedelta(hours=1), end_datetime=F("end_datetime") + timedelta(hours=1))
    result = build_confirm_payload(booking, link, approved_on=timezone.now())
    assert "allocated_timings_from" in result.errors and result.payload == {}
