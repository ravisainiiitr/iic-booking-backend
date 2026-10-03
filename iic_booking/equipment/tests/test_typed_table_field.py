"""Advanced table (TYPED_TABLE) dynamic input fields: per-column types and limits, user-managed rows, and
rows linked to a numeric field, on every path that accepts input values."""

from __future__ import annotations

import json
import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.admin import DynamicInputFieldForm
from iic_booking.equipment.calculators import SAMPLE_SETS_KEY, build_safe_input_values_for_charge_calculation
from iic_booking.equipment.models import Booking, DynamicInputField, DynamicInputFieldType, Equipment
from iic_booking.equipment.template_health import check_values
from iic_booking.equipment.tests.test_equipment_create_paths import _admin, _dept, _portal_payload
from iic_booking.equipment.tests.test_sample_set_combined_limits import (  # noqa: F401 - fixture
    _book,
    _patch,
    _student_with_wallet,
    no_portal_lock,
)
from iic_booking.equipment.typed_table import (
    TableConfigError,
    clean_table_rows,
    clean_typed_tables,
    format_typed_table_text,
    normalize_table_config,
    restore_typed_tables,
    trim_linked_typed_tables,
    validate_table_links,
)

COLUMNS = [
    {"label": "Sample code", "type": "TEXT", "required": True, "max_length": 10},
    {"label": "Max temperature", "key": "temp", "type": "NUMERIC", "min": 0, "max": 100, "step": 0.5},
    {"label": "Count", "type": "NUMERIC", "integer": True, "min": 1, "max": 5},
    {"label": "Phase", "type": "RADIO", "options": ["Solid", "Liquid"]},
    {"label": "Gas", "type": "COMBO", "options": ["N2", "Ar"]},
    {"label": "Tests", "type": "MULTI_SELECT", "options": ["XRD", "SEM", "TEM"]},
    {"label": "Toxic", "type": "TOGGLE"},
    {"label": "Elements", "type": "PERIODIC_TABLE"},
]


def _config(mode="USER", **rows):
    return normalize_table_config({"columns": COLUMNS, "rows": {"mode": mode, **rows}})


def _field(**kwargs):
    defaults = {"field_key": "C", "field_label": "Samples", "is_required": False, "table_config": _config()}
    defaults.update(kwargs)
    return DynamicInputField(field_type=DynamicInputFieldType.TYPED_TABLE, **defaults)


def _equipment(egs_factory, *, mode="LINKED", time_formula="30", **rows):
    eq = egs_factory.equipment(time_formula=time_formula)
    DynamicInputField.objects.create(
        equipment=eq, field_key="A", field_label="No. of Samples", field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10}, default_value="1", editing_required=True,
    )
    link = {"link_field_key": "A"} if mode == "LINKED" else {}
    DynamicInputField.objects.create(
        equipment=eq, field_key="C", field_label="Sample details", field_type=DynamicInputFieldType.TYPED_TABLE,
        table_config=_config(mode, **link, **rows), editing_required=True,
    )
    return eq


def _row(code="S1", **cells):
    return {"sample_code": code, **cells}


# --- schema -------------------------------------------------------------------------------------------


def test_schema_is_normalised_with_derived_keys_and_defaults():
    cfg = _config()
    keys = [c["key"] for c in cfg["columns"]]
    assert keys == ["sample_code", "temp", "count", "phase", "gas", "tests", "toxic", "elements"]
    assert cfg["columns"][1]["min"] == 0 and cfg["columns"][1]["max"] == 100 and cfg["columns"][1]["step"] == 0.5
    assert cfg["rows"] == {
        "mode": "USER", "link_field_key": None, "min_rows": 0, "max_rows": 50, "initial_rows": 1,
        "serial_column": True, "allow_duplicate": True,
    }
    linked = _config("LINKED", link_field_key="a", max_rows=20)
    assert linked["rows"]["link_field_key"] == "A"
    assert linked["rows"]["min_rows"] == 0 and linked["rows"]["max_rows"] == 20


@pytest.mark.parametrize(
    "raw,message",
    [
        ({"columns": []}, "at least one column"),
        ({"columns": [{"label": "X", "key": "x"}, {"label": "Y", "key": "x"}]}, 'Column key "x" is used by more'),
        ({"columns": [{"label": "T", "type": "NUMERIC", "min": 5, "max": 1}]}, "lower limit cannot be greater"),
        ({"columns": [{"label": "P", "type": "RADIO"}]}, "needs at least one option"),
        ({"columns": [{"label": "P", "type": "RADIO", "options": ["a", "a"]}]}, "listed twice"),
        ({"columns": [{"label": "T", "type": "NUMERIC", "max": 5, "default": 9}]}, "default value"),
        ({"columns": [{"label": "X"}], "rows": {"mode": "LINKED"}}, "sets the number of rows"),
        ({"columns": [{"label": "X"}], "rows": {"min_rows": 5, "max_rows": 2}}, "Minimum rows cannot be greater"),
        ({"columns": [{"label": "X", "type": "DATE"}]}, "unknown type"),
        ({"columns": [{"label": "X", "key": "1bad"}]}, "must start with a letter"),
        ({"columns": [{"label": "X"}], "rows": {"max_rows": 500}}, "between 1 and 200"),
    ],
)
def test_invalid_schemas_are_rejected(raw, message):
    with pytest.raises(TableConfigError) as exc:
        normalize_table_config(raw)
    assert message in str(exc.value)


def test_link_must_point_at_a_numeric_field_of_the_same_group():
    linked = {"field_key": "C", "field_label": "T", "field_type": "TYPED_TABLE",
              "table_config": _config("LINKED", link_field_key="A")}
    numeric = {"field_key": "A", "field_type": "NUMERIC"}
    assert validate_table_links([numeric, linked]) is None
    assert "does not exist" in validate_table_links([linked])
    assert "must be a Numeric field" in validate_table_links([{"field_key": "A", "field_type": "TEXT"}, linked])
    self_link = {**linked, "table_config": _config("LINKED", link_field_key="C")}
    assert "cannot be linked to the table itself" in validate_table_links([numeric, self_link])


@pytest.mark.django_db
def test_equipment_form_api_saves_and_validates_the_schema():
    client = APIClient()
    client.force_authenticate(user=_admin())
    code = f"TT{uuid.uuid4().hex[:5].upper()}"
    payload = _portal_payload(code, _dept())
    numeric = {"user_type": "student", "field_key": "A", "field_label": "No. of Samples", "field_type": "NUMERIC",
               "is_required": True, "default_value": "1", "options": {"min": 1, "max": 10}, "help_text": ""}
    table = {"user_type": "student", "field_key": "C", "field_label": "Samples", "field_type": "TYPED_TABLE",
             "table_config": {"columns": COLUMNS, "rows": {"mode": "LINKED", "link_field_key": "A"}}}

    payload["input_fields"] = [numeric, {**table, "table_config": {"columns": [{"label": "P", "type": "RADIO"}]}}]
    bad = client.post("/api/admin/equipment/", payload, format="json")
    assert bad.status_code == 400
    assert "needs at least one option" in str(bad.data)

    payload["input_fields"] = [{**table}]
    unlinked = client.post("/api/admin/equipment/", payload, format="json")
    assert unlinked.status_code == 400
    assert "linked field A does not exist" in str(unlinked.data)

    payload["input_fields"] = [numeric, table]
    ok = client.post("/api/admin/equipment/", payload, format="json")
    assert ok.status_code == 201, getattr(ok, "data", ok.content[:2000])
    stored = DynamicInputField.objects.get(equipment__code=code, field_key="C")
    assert stored.table_config["rows"]["link_field_key"] == "A"
    assert stored.source_element_field_key == "A"
    assert stored.table_config["columns"][0]["key"] == "sample_code"

    eq = Equipment.objects.get(code=code)
    detail = client.get(f"/api/admin/equipment/{eq.pk}/", {"all_input_fields": 1})
    assert detail.status_code == 200
    fields = detail.data.get("input_fields") or []
    assert any(f["field_key"] == "C" and f["table_config"]["rows"]["mode"] == "LINKED" for f in fields)


def test_django_admin_form_validates_and_stores_the_schema():
    base = {"field_key": "C", "field_label": "Samples", "field_type": "TYPED_TABLE", "user_type": "",
            "options_text": "", "help_text": "", "default_value": ""}
    bad = DynamicInputFieldForm(data={**base, "table_config": json.dumps({"columns": [{"label": "P", "type": "RADIO"}]})})
    assert not bad.is_valid()
    assert "needs at least one option" in str(bad.errors["options_text"])

    good = DynamicInputFieldForm(data={**base, "table_config": json.dumps(
        {"columns": COLUMNS, "rows": {"mode": "LINKED", "link_field_key": "A"}}
    )})
    assert good.is_valid(), good.errors
    assert good.cleaned_data["table_config"]["rows"]["link_field_key"] == "A"
    assert good.cleaned_data["source_element_field_key"] == "A"

    plain = DynamicInputFieldForm(data={**base, "field_type": "TEXT", "table_config": "{}"})
    assert plain.is_valid(), plain.errors
    assert plain.cleaned_data["table_config"] == {}


# --- cells and rows -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cells,kind,fragment",
    [
        ({"temp": 120}, "max", "Max temperature cannot be greater than 100."),
        ({"temp": -1}, "min", "Max temperature cannot be less than 0."),
        ({"temp": "hot"}, "invalid", "must be a number"),
        ({"count": 2.5}, "integer", "Count must be a whole number."),
        ({"phase": "Gas"}, "option", '"Gas" is not one of the options'),
        ({"tests": ["XRD", "NMR"]}, "option", '"NMR" is not one of the options'),
        ({"sample_code": "X" * 11}, "max_length", "at most 10 characters"),
        ({"elements": ["Xx"]}, "invalid", "not an element symbol"),
        ({"toxic": "maybe"}, "invalid", "Yes or No"),
    ],
)
def test_each_column_type_is_validated(cells, kind, fragment):
    rows, problems = clean_table_rows(_field(), [_row(**cells)], {})
    assert problems, rows
    assert problems[0]["kind"] == kind
    assert problems[0]["row"] == 1
    assert fragment in problems[0]["message"]
    assert problems[0]["message"].startswith("Samples, row 1: ")


def test_valid_cells_are_cleaned_and_blank_rows_dropped():
    raw = [
        {"sample_code": " S1 ", "temp": "40.5", "count": "2", "phase": "Solid", "gas": "Ar",
         "tests": "XRD, SEM", "toxic": "yes", "elements": ["fe", "Co"], "unknown": "x"},
        {"sample_code": "", "temp": None, "toxic": False},
    ]
    rows, problems = clean_table_rows(_field(), raw, {})
    assert problems == []
    assert rows == [{
        "sample_code": "S1", "temp": 40.5, "count": 2, "phase": "Solid", "gas": "Ar", "tests": ["XRD", "SEM"],
        "toxic": True, "elements": ["Fe", "Co"],
    }]


def test_required_cells_minimum_and_maximum_rows():
    field = _field(table_config=_config(min_rows=2, max_rows=3))
    _rows, problems = clean_table_rows(field, [_row(), {"temp": 5}], {})
    assert [p["kind"] for p in problems] == ["required"]
    assert "row 2: Sample code is required" in problems[0]["message"]

    _rows, problems = clean_table_rows(field, [_row()], {})
    assert problems[0]["kind"] == "min_rows" and problems[0]["limit"] == 2

    _rows, problems = clean_table_rows(field, [_row(f"S{i}") for i in range(4)], {})
    assert problems[0]["kind"] == "max_rows" and problems[0]["limit"] == 3

    # Templates skip required cells and the minimum but keep the maximum.
    _rows, problems = clean_table_rows(field, [{"temp": 5}], {}, check_required=False)
    assert problems == []


def test_linked_rows_must_match_the_linked_value_capped_at_max_rows():
    field = _field(table_config=_config("LINKED", link_field_key="A", max_rows=3))
    assert clean_table_rows(field, [_row(), _row("S2")], {"A": 2})[1] == []
    problems = clean_table_rows(field, [_row()], {"A": 2})[1]
    assert problems[0]["kind"] == "row_count"
    assert problems[0]["message"] == "Samples must have 2 rows (set by field A); it has 1."
    assert clean_table_rows(field, [_row(f"S{i}") for i in range(3)], {"A": 9})[1] == []
    assert clean_table_rows(field, [], {})[1] == []


# --- booking paths ------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_requires_linked_rows_per_sample_set_and_stores_clean_rows(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student = _student_with_wallet(egs_factory)
    slot = egs_factory.slot(eq, egs_factory.future())

    short = _book(egs_factory, student, eq, slot, {"A": 2, "C": [_row()]})
    assert short.status_code == 400
    assert short.data["error"] == "Sample details must have 2 rows (set by field A); it has 1."

    bad_set = _book(egs_factory, student, eq, slot, {
        "A": 1, "C": [_row()], SAMPLE_SETS_KEY: [{"A": 1, "C": [_row(temp=500)]}],
    })
    assert bad_set.status_code == 400
    assert bad_set.data["error"].startswith("Sample set 2: Sample details, row 1: Max temperature cannot be")
    assert not Booking.objects.filter(user=student).exists()

    ok = _book(egs_factory, student, eq, slot, {
        "A": 2, "C": [_row(temp="40"), _row("S2", phase="Liquid")], SAMPLE_SETS_KEY: [{"A": 1, "C": [_row("S3")]}],
    })
    assert ok.status_code == 201, ok.data
    booking = Booking.objects.get(user=student)
    assert booking.input_values["C"] == [_row(temp=40, toxic=False), _row("S2", phase="Liquid", toxic=False)]
    assert booking.input_values[SAMPLE_SETS_KEY][0]["C"] == [_row("S3", toxic=False)]


@pytest.mark.django_db
def test_edit_validates_cells_and_keeps_unchanged_legacy_rows(egs_factory):
    eq = _equipment(egs_factory, mode="USER")
    owner = egs_factory.student()
    legacy = [{"sample_code": "TOO-LONG-CODE", "temp": 500}]
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 1, "C": legacy})

    unchanged = _patch(egs_factory, owner, booking, {"A": 2, "C": legacy})
    assert unchanged.status_code == 200, unchanged.data

    over = _patch(egs_factory, owner, booking, {"A": 2, "C": [_row(temp=101)]})
    assert over.status_code == 400
    assert "Max temperature cannot be greater than 100." in over.data["error"]

    fixed = _patch(egs_factory, owner, booking, {"A": 2, "C": [_row(temp=99)]})
    assert fixed.status_code == 200, fixed.data
    booking.refresh_from_db()
    assert booking.input_values["C"] == [_row(temp=99, toxic=False)]


@pytest.mark.django_db
def test_table_key_stands_for_filled_rows_in_formulas(egs_factory):
    eq = _equipment(egs_factory, mode="USER", time_formula="C*30")
    safe = build_safe_input_values_for_charge_calculation(
        {"A": 1, "C": [_row(), _row("S2"), {"sample_code": ""}], SAMPLE_SETS_KEY: [{"A": 1, "C": [_row()]}]},
        equipment=eq,
    )
    assert safe["C"] == 2
    assert safe[SAMPLE_SETS_KEY][0]["C"] == 1

    estimate = egs_factory.client_for(egs_factory.student()).get(
        f"/api/equipments/{eq.pk}/calculate/", {"A": 1, "C": 3}
    )
    assert estimate.status_code == 200, estimate.data
    assert int(estimate.data["total_time_minutes"]) == 90


@pytest.mark.django_db
def test_partial_cancel_and_waitlist_keep_linked_rows_in_step(egs_factory):
    eq = _equipment(egs_factory)
    values = {"A": 2, "C": [_row(), _row("S2")], SAMPLE_SETS_KEY: [{"A": 3, "C": [_row("a"), _row("b"), _row("c")]}]}

    trimmed = trim_linked_typed_tables(eq, {**values, "A": 1})
    assert trimmed["C"] == [_row()]
    assert len(trimmed[SAMPLE_SETS_KEY][0]["C"]) == 3

    safe = build_safe_input_values_for_charge_calculation({**values, "A": 1}, equipment=eq)
    restored = restore_typed_tables(eq, values, safe)
    assert restored["C"] == [_row()]
    assert restored[SAMPLE_SETS_KEY][0]["C"] == values[SAMPLE_SETS_KEY][0]["C"]


@pytest.mark.django_db
def test_clean_typed_tables_ignores_equipment_without_advanced_tables(egs_factory):
    eq = egs_factory.equipment()
    values = {"A": 1, "C": [{"x": 1}]}
    assert clean_typed_tables(eq, values) == (values, None)


def test_plain_text_rendering():
    text = format_typed_table_text(_config(), [_row(temp=40, toxic=True, tests=["XRD", "SEM"])])
    assert text == "1. Sample code: S1; Max temperature: 40; Tests: XRD, SEM; Toxic: Yes"


# --- templates ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_template_save_blocks_out_of_range_cells_but_allows_unfinished_rows(egs_factory):
    eq = _equipment(egs_factory)
    user = egs_factory.student()
    client = egs_factory.client_for(user)

    bad = client.post("/api/booking-templates/", {
        "equipment": eq.pk, "name": "Hot", "input_values": {"A": 2, "C": [_row(), _row("S2", temp=150)]},
    }, format="json")
    assert bad.status_code == 400
    assert bad.data["error"] == "Sample details, row 2: Max temperature cannot be greater than 100."
    assert bad.data["error_field"] == {"field": "C", "set": 1, "kind": "max", "limit": 100, "row": 2, "column": "temp"}

    unfinished = client.post("/api/booking-templates/", {
        "equipment": eq.pk, "name": "Draft", "input_values": {"A": 2, "C": [{"temp": "20"}]},
    }, format="json")
    assert unfinished.status_code == 201, unfinished.data
    assert unfinished.data["input_values"]["C"] == [{"temp": 20, "toxic": False}]


@pytest.mark.django_db
def test_template_health_reports_invalid_and_incomplete_tables(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    student = _student_with_wallet(egs_factory)

    health = check_values(student, eq, {"A": 2, "C": [_row(temp=150)]}, {}, None, light=True)
    codes = {i["code"]: i for i in health["issues"]}
    assert codes["table_invalid"]["field"] == "C"
    assert codes["table_invalid"]["kind"] == "max"
    assert codes["table_incomplete"]["kind"] == "row_count"
    assert health["status"] == "needs_attention"

    ok = check_values(student, eq, {"A": 1, "C": [_row()]}, {}, None, light=True)
    assert not [i for i in ok["issues"] if i["code"].startswith("table_")]
