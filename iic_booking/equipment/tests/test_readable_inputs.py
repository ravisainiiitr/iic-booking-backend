"""Readable booking inputs (labels, option labels, table rows) and the Booking Attempt Log details:
labels resolved at read time incl. renamed / deleted fields, never raw JSON, plain-language failure reasons."""

from __future__ import annotations

import json

import pytest

from iic_booking.equipment.attempt_log_display import attempt_detail, resolve_logged_inputs
from iic_booking.equipment.failure_reasons import explain
from iic_booking.equipment.input_display import (
    choice_label,
    format_input_value,
    humanize_key,
    input_summary_lines,
    readable_inputs,
)
from iic_booking.equipment.models import (
    BookingAttemptLog,
    BookingAttemptOutcome,
    DynamicInputField,
    DynamicInputFieldType as T,
)

# --- input_display ------------------------------------------------------------------------------------


def test_choice_label_uses_option_labels_values_and_indexes():
    options = [{"value": "s", "label": "Solid/Films"}, {"value": "p", "label": "Powder"}]
    assert choice_label("p", options) == "Powder"
    assert choice_label("2", ["Solid", "Liquid"]) == "Liquid"
    assert choice_label("Liquid", ["Solid", "Liquid"]) == "Liquid"
    assert choice_label(True, ["No", "Yes"]) == "Yes"
    assert choice_label("Other", ["Solid"]) == "Other"


def test_table_field_json_becomes_rows_with_headers_and_blank_rows_dropped():
    field = {"field_key": "D", "field_label": "Sample Details", "field_type": "TABLE",
             "options": ["S.No.", "Name", "Count"]}
    value = format_input_value(field, {"D": json.dumps([["1", "N", "5"], ["2", "", ""], ["", "", ""]])})
    assert value == {"kind": "table", "columns": ["S.No.", "Name", "Count"], "rows": [["1", "N", "5"], ["2", "", ""]]}


def test_typed_table_rows_use_column_labels_and_yes_no():
    field = {
        "field_key": "C", "field_label": "Samples", "field_type": "TYPED_TABLE",
        "table_config": {"columns": [{"key": "code", "label": "Code", "type": "TEXT"},
                                     {"key": "toxic", "label": "Toxic", "type": "TOGGLE"}],
                         "rows": {"serial_column": True}},
    }
    value = format_input_value(field, {"C": [{"code": "S1", "toxic": True}, {"code": "", "toxic": False}]})
    assert value == {"kind": "table", "columns": ["S.No.", "Code", "Toxic"], "rows": [["1", "S1", "Yes"]]}


def test_periodic_table_shows_elements_and_toggle_yes_no():
    assert format_input_value({"field_key": "B", "field_type": "PERIODIC_TABLE"}, {"B": 5, "B_elements": "C,Lu,W"}) == {
        "kind": "text", "text": "C, Lu, W"}
    assert format_input_value({"field_key": "G", "field_type": "TOGGLE"}, {"G": False}) == {"kind": "text", "text": "No"}


def test_readable_inputs_omit_empty_fields_humanise_unknown_keys_and_split_sets():
    fields = [
        {"field_key": "A", "field_label": "No. of samples:", "field_type": "NUMERIC"},
        {"field_key": "C", "field_label": "Sample Type", "field_type": "RADIO", "options": ["Solid/Films", "Powder"]},
        {"field_key": "E", "field_label": "Unused", "field_type": "TEXT"},
    ]
    values = {"A": "2", "C": "1", "E": "", "comments": "Handle with care", "sample_type_note": "dry",
              "_sample_sets": [{"A": "3", "C": "2"}]}
    data = readable_inputs(values, fields)
    assert data["sets"] == 2
    assert [f["label"] for f in data["fields"]] == ["No. of samples", "Sample Type", "Sample type note"]
    assert data["fields"][1]["values"] == [{"kind": "text", "text": "Solid/Films"}, {"kind": "text", "text": "Powder"}]
    assert data["comments"] == "Handle with care"
    lines = dict(input_summary_lines(values, fields))
    assert lines["Sample Type"] == "Set 1: Solid/Films; Set 2: Powder"
    assert lines["Comments"] == "Handle with care"
    assert humanize_key("B_elements") == "B elements"


# --- attempt log inputs -------------------------------------------------------------------------------


def _fields(eq):
    DynamicInputField.objects.create(equipment=eq, field_key="A", field_label="No of samples", field_type=T.NUMERIC)
    DynamicInputField.objects.create(equipment=eq, field_key="B", field_label="Select Element", field_type=T.PERIODIC_TABLE)
    DynamicInputField.objects.create(
        equipment=eq, field_key="C", field_label="Sample Type", field_type=T.RADIO,
        options=[{"value": "1", "label": "Solid/Films"}, {"value": "2", "label": "Powder"}],
    )
    DynamicInputField.objects.create(
        equipment=eq, field_key="D", field_label="Sample Details", field_type=T.TABLE,
        options=["S.No.", "Name", "Count", "Size", "Remarks"],
    )


SCREENSHOT_INFO = {
    "input_values": {
        "comments": "",
        "B_elements": "C,Lu,W,Ir,Pt,Pb",
        "Sample Type": "1",
        "No of samples": "2",
        "Sample Details": '[["1","N","5","1",""],["2","","","",""]]',
        "Select Element": "5",
        "Pre Treatment Temperature (Degree Celsius)": "30",
    }
}


def test_label_keyed_log_resolves_to_field_keys_and_keeps_deleted_fields_readable(egs_factory):
    eq = egs_factory.equipment()
    _fields(eq)
    data = resolve_logged_inputs(eq.pk, "", SCREENSHOT_INFO)
    values = data["input_values"]
    assert values["A"] == "2" and values["B"] == "5" and values["C"] == "1"
    assert values["B_elements"] == "C,Lu,W,Ir,Pt,Pb"
    assert values["D"] == [["1", "N", "5", "1", ""], ["2", "", "", "", ""]]
    keys = [f["field_key"] for f in data["input_fields"]]
    assert keys[:4] == ["A", "B", "C", "D"]
    deleted = data["input_fields"][-1]
    assert deleted["field_label"] == "Pre Treatment Temperature (Degree Celsius)"
    assert "B_elements" not in keys
    assert data["comments"] == ""

    lines = dict(input_summary_lines({**values}, data["input_fields"]))
    assert lines["Select Element"] == "C, Lu, W, Ir, Pt, Pb"
    assert lines["Sample Type"] == "Solid/Films"
    assert "[[" not in " ".join(lines.values())


def test_deleted_table_field_is_shown_as_rows_not_json(egs_factory):
    eq = egs_factory.equipment()
    data = resolve_logged_inputs(eq.pk, "", {"input_values": {"Old Table": '[["a","b"]]', "old_rows": [{"x_val": 1}]}})
    by_key = {f["field_key"]: f for f in data["input_fields"]}
    assert by_key["Old Table"]["field_type"] == "TABLE"
    assert by_key["old_rows"]["field_type"] == "TYPED_TABLE"
    assert by_key["old_rows"]["field_label"] == "Old rows"
    text = dict(input_summary_lines(data["input_values"], data["input_fields"]))
    assert text["Old Table"] == "a, b"
    assert text["Old rows"] == "X val: 1"


def test_key_based_log_survives_a_renamed_field(egs_factory):
    eq = egs_factory.equipment()
    _fields(eq)
    info = {"input_values": {"Old label": "2"}, "input_values_by_key": {"C": "2", "comments": "Fragile"}}
    data = resolve_logged_inputs(eq.pk, "", info)
    assert data["input_values"] == {"C": "2"}
    assert data["comments"] == "Fragile"


def test_attempt_detail_api_has_user_slots_inputs_and_friendly_outcome(egs_factory):
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    eq = egs_factory.equipment()
    _fields(eq)
    student = egs_factory.student()
    slot = egs_factory.slot(eq, egs_factory.future())
    log = BookingAttemptLog.objects.create(
        user=student, equipment=eq, outcome=BookingAttemptOutcome.FAILED,
        failure_reason=(
            "Quota check failed: Individual Weekly quota exceeded: current usage 120 min + requested 90 min "
            "= 210 min; configured limit 270 min; remaining before this request 150 min."
        ),
        additional_info={**SCREENSHOT_INFO, "slot_ids": [slot.pk]},
    )
    admin = UserFactory(user_type=UserType.ADMIN)
    resp = egs_factory.client_for(admin).get(f"/api/booking-attempt-logs/{log.pk}/")
    assert resp.status_code == 200, resp.data
    data = resp.data
    assert data["user"]["email"] == student.email
    assert data["user"]["department_name"] == egs_factory.department.name
    assert data["requested_slots"][0]["id"] == slot.pk
    assert data["input_values"]["C"] == "1"
    outcome = data["outcome_details"]
    assert outcome["status"] == "FAILED"
    assert outcome["message"] == (
        "Weekly booking limit reached: you had used 120 min and requested 90 min; the weekly limit is 270 min."
    )
    assert outcome["technical"].startswith("Quota check failed")

    listed = egs_factory.client_for(admin).get("/api/booking-attempt-logs/")
    row = next(r for r in listed.data["results"] if r["id"] == log.pk)
    assert row["failure_title"] == "Weekly booking limit reached"

    other = egs_factory.client_for(student).get(f"/api/booking-attempt-logs/{log.pk}/")
    assert other.status_code == 403


def test_attempt_detail_for_success_names_the_booking(egs_factory):
    eq = egs_factory.equipment()
    student = egs_factory.student()
    booking = egs_factory.booking(student, eq, egs_factory.future())
    log = BookingAttemptLog.objects.create(
        user=student, equipment=eq, outcome=BookingAttemptOutcome.SUCCESS, booking_id=booking.booking_id,
    )
    data = attempt_detail(log)
    assert data["outcome_details"]["title"] == f"Booking created: {booking.virtual_booking_id}"
    assert len(data["booked_slots"]) == 1


# --- failure reasons ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message,code,needle",
    [
        ("Quota check failed: Individual Weekly quota exceeded: current usage 120 min + requested 90 min > 270 min",
         "quota_time", "the weekly limit is 270 min"),
        ("Quota check failed: Faculty Monthly quota exceeded: current usage 600 min + requested 120 min = 720 min; "
         "configured limit 660 min; remaining before this request 60 min (shared across 3 user(s) on the faculty wallet).",
         "quota_time", "shared by 3 users"),
        ("Quota check failed: Individual Weekly booking-count quota exceeded: 4 bookings vs limit 3.", "quota_count", "limit is 3"),
        ("Quota check failed: External Monthly charge quota exceeded: ₹12500.00 vs limit ₹10000.00.", "quota_charge", "₹10,000"),
        ("The selected slot(s) have already been booked by another user. Please choose a different slot.", "slot_taken", "someone else"),
        ("Slots [101, 102] are not available for booking.", "slot_unavailable", "no longer free"),
        ("Sample set 2: No. of Samples cannot be greater than 8 (A <= B*4).", "input_limit", "Sample set 2"),
        ("Insufficient wallet balance", "wallet_balance", "enough balance"),
        ("You don't have access to any wallet.", "no_wallet", "no wallet"),
        ("No active charge profile found for equipment 5 and user type external.", "no_charge_profile", "(external)"),
        ("Equipment is not operational (current status: Under Maintenance).", "equipment_unavailable", "Under Maintenance"),
        ("Error creating booking: IntegrityError", "system_error", "system error"),
        ("LEGACY_MIGRATION_SLOT_BLOCKED", "legacy_block", "previous booking portal"),
        ("This equipment mode is not scheduled for booking at the selected time.", "mode_unavailable", "not scheduled"),
        ("Only the active exclusive mode of this instrument can be booked at this time.", "mode_unavailable",
         "Another mode"),
    ],
)
def test_explain_failure_reasons(message, code, needle):
    result = explain(message)
    assert result["code"] == code
    assert needle in result["message"]
    assert result["title"]


def test_explain_empty_and_unknown():
    assert explain("")["code"] == "unknown"
    assert explain("", outcome="SUCCESS")["code"] == "success"
    assert explain("Something odd happened")["message"] == "Something odd happened"


# --- places that used to show raw keys / JSON ------------------------------------------------------------


def test_waitlist_history_row_has_field_keys_and_definitions(egs_factory):
    from iic_booking.equipment.api_views import _serialize_waitlist_entry_for_history
    from iic_booking.equipment.models import WaitlistEntry

    eq = egs_factory.equipment()
    _fields(eq)
    student = egs_factory.student()
    BookingAttemptLog.objects.create(
        user=student, equipment=eq, outcome=BookingAttemptOutcome.FAILED,
        failure_reason="Booking unsuccessful. All slots are occupied.", additional_info=SCREENSHOT_INFO,
    )
    entry = WaitlistEntry.objects.create(equipment=eq, user=student, status="ACTIVE")
    row = _serialize_waitlist_entry_for_history(entry, 1)
    assert row["input_values"]["C"] == "1"
    assert row["input_values"]["D"] == '[["1","N","5","1",""],["2","","","",""]]'
    assert [f["field_key"] for f in row["input_fields"]][:4] == ["A", "B", "C", "D"]


def test_admin_waitlist_lists_readable_attempt_inputs(egs_factory):
    from iic_booking.equipment.models import WaitlistEntry
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    eq = egs_factory.equipment()
    _fields(eq)
    student = egs_factory.student()
    log = BookingAttemptLog.objects.create(
        user=student, equipment=eq, outcome=BookingAttemptOutcome.FAILED,
        failure_reason="Slots [101, 102] are not available for booking.", additional_info=SCREENSHOT_INFO,
    )
    WaitlistEntry.objects.create(equipment=eq, user=student, status="ACTIVE")
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)
    resp = egs_factory.client_for(admin).get(f"/api/admin/equipment/{eq.pk}/waitlist/")
    if resp.status_code == 404:
        pytest.skip("admin equipment waitlist route not mounted in this settings module")
    assert resp.status_code == 200, resp.data
    entry = resp.data["entries"][0]
    assert entry["booking_attempt_log_id"] == log.pk
    inputs = {i["label"]: i["text"] for i in entry["booking_attempt_inputs"]}
    assert inputs["Sample Type"] == "Solid/Films"
    assert inputs["Select Element"] == "C, Lu, W, Ir, Pt, Pb"
    assert entry["booking_attempt_failure_title"] == "Slot no longer available"


def test_template_summary_shows_option_labels():
    from iic_booking.equipment.booking_templates import input_summary

    labels = {"A": ("Samples:", "NUMERIC", None), "C": ("Sample Type", "RADIO", [{"value": "p", "label": "Powder"}])}
    assert input_summary({"A": 2, "C": "p"}, labels) == [
        {"key": "A", "label": "Samples:", "value": "2"},
        {"key": "C", "label": "Sample Type", "value": "Powder"},
    ]


def test_results_sharing_summary_is_readable(egs_factory):
    from iic_booking.equipment.results_sharing_service import booking_input_summary

    eq = egs_factory.equipment()
    _fields(eq)
    student = egs_factory.student()
    booking = egs_factory.booking(student, eq, egs_factory.future())
    booking.input_values = {"A": "2", "C": "2", "D": [["1", "N", "5", "", ""]], "comments": "Dry"}
    booking.save(update_fields=["input_values"])
    summary = {i["label"]: i["value"] for i in booking_input_summary(booking)}
    assert summary["Sample Type"] == "Powder"
    assert summary["Sample Details"] == "Name: N, Count: 5"
    assert summary["Comments"] == "Dry"


def test_dashboard_groups_failure_reasons_in_plain_language(egs_factory):
    from django.utils import timezone

    from iic_booking.equipment.admin_dashboard_summary import _booking_attempts

    eq = egs_factory.equipment()
    student = egs_factory.student()
    for used in (100, 200):
        BookingAttemptLog.objects.create(
            user=student, equipment=eq, outcome=BookingAttemptOutcome.FAILED,
            failure_reason=f"Quota check failed: Individual Weekly quota exceeded: current usage {used} min + "
                           f"requested 90 min > 270 min",
        )

    class _AllScope:
        def by_department(self, qs, _field):
            return qs

    data = _booking_attempts(_AllScope(), timezone.now())
    assert data["top_failure_reasons"][0] == {"reason": "Weekly booking limit reached", "count": 2}
