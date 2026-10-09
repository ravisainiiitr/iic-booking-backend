"""TGA/DTA field C: Sample Name/Code column, one advanced table for every user type, stored values converted."""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from iic_booking.equipment.calculators import SAMPLE_SETS_KEY
from iic_booking.equipment.input_display import format_input_value
from iic_booking.equipment.models import (
    Booking,
    BookingInputTemplate,
    ChargeProfile,
    DynamicInputField,
    DynamicInputFieldType as T,
    EquipmentProfileType,
)
from iic_booking.equipment.tests.test_sample_set_combined_limits import _patch
from iic_booking.equipment.tga_field_c import LEGACY_COLUMNS, NEW_KEY, convert_table_value, restore, run
from iic_booking.equipment.typed_table import normalize_table_config

OLD_COLUMNS = [
    {"key": "initial_temp_c", "label": "Initial Temp. (°C)", "type": "NUMERIC", "min": 30, "max": 1350, "step": 1,
     "integer": True, "default": 30, "required": True},
    {"key": "final_temp_c", "label": "Final Temp. (°C)", "type": "NUMERIC", "min": 30, "max": 1350, "step": 1,
     "integer": True, "default": 800, "required": True},
    {"key": "rate_c_min", "label": "Rate (°C/min)", "type": "NUMERIC", "min": 1, "max": 100, "step": 1,
     "integer": True, "default": 10, "required": True},
    {"key": "hold_min", "label": "Hold (min)", "type": "NUMERIC", "min": 0, "max": 100, "step": 1,
     "integer": True, "default": 0, "required": True},
    {"key": "atmosphere", "label": "Atmosphere", "type": "COMBO", "options": ["Air", "Nitrogen"],
     "default": "Nitrogen", "required": True},
    {"key": "flow_rate_ml_min", "label": "Flow rate (ml/min)", "type": "NUMERIC", "min": 150, "max": 200, "step": 1,
     "integer": True, "default": 150, "required": True},
]
OLD_CONFIG = normalize_table_config({"columns": OLD_COLUMNS, "rows": {"mode": "USER", "min_rows": 1, "initial_rows": 1}})
ROW = {"initial_temp_c": 30, "final_temp_c": 800, "rate_c_min": 10, "hold_min": 0, "atmosphere": "Nitrogen",
       "flow_rate_ml_min": 150}
LEGACY_DATA = [["1", "S-1", "600", "10", "N2", "TGA", ""]]
LEGACY_BLANK = [["1", "", "", "", "", "", ""], ["2", "", "", "", "", "", ""]]


def _tga(egs_factory, code="TGA/DTA [A]"):
    eq = egs_factory.equipment(code=code, time_formula="A*60", unit_charge="100.00")
    ChargeProfile.objects.create(equipment=eq, user_type="external", profile_type=EquipmentProfileType.HOUR,
                                 time_formula="A*60", primary_unit_charge=Decimal("1000.00"))
    for user_type in ("student", "faculty", "external"):
        DynamicInputField.objects.create(equipment=eq, user_type=user_type, field_key="A", field_label="No. of Samples",
                                         field_type=T.NUMERIC, is_required=True, default_value="1")
    for user_type in ("student", "faculty"):
        DynamicInputField.objects.create(equipment=eq, user_type=user_type, field_key="C", field_label="Samples Details",
                                         field_type=T.TYPED_TABLE, table_config=OLD_CONFIG, editing_required=True)
    DynamicInputField.objects.create(equipment=eq, user_type="external", field_key="C", field_label="Samples Details",
                                     field_type=T.TABLE, options=LEGACY_COLUMNS, editing_required=True)
    return eq


def test_values_get_a_blank_sample_name_and_plain_tables_are_held_back_unless_mapped():
    config = normalize_table_config({"columns": [{"key": NEW_KEY, "label": "Sample Name/Code", "required": True},
                                                 *OLD_COLUMNS]})
    assert convert_table_value([ROW], config) == ([{NEW_KEY: "", **ROW}], "advanced", {})
    done = [{NEW_KEY: "S1", **ROW}]
    assert convert_table_value(done, config)[1] == "advanced-done"
    assert convert_table_value(LEGACY_BLANK, config)[:2] == ([{NEW_KEY: ""}, {NEW_KEY: ""}], "simple-blank")
    value, kind, lost = convert_table_value(LEGACY_DATA, config)
    assert (value, kind, dict(lost)) == (LEGACY_DATA, "simple-data", {LEGACY_COLUMNS[5]: 1})
    mapped, kind, _lost = convert_table_value(LEGACY_DATA, config, simple_mapping=True)
    assert kind == "simple-mapped"
    assert mapped == [{NEW_KEY: "S-1", "final_temp_c": 600, "rate_c_min": 10, "atmosphere": "Nitrogen"}]
    assert convert_table_value([], config)[1] == "empty"


@pytest.mark.django_db
def test_run_changes_every_profile_converts_values_and_restores(egs_factory):
    eq = _tga(egs_factory)
    owner = egs_factory.student()
    advanced = egs_factory.booking(owner, eq, egs_factory.future(), total_charge="300.00", input_values={
        "A": 2, "C": [ROW, ROW], SAMPLE_SETS_KEY: [{"A": 1, "C": [ROW]}]})
    blank = egs_factory.booking(owner, eq, egs_factory.future(days=4), input_values={"A": 1, "C": LEGACY_BLANK})
    held = egs_factory.booking(owner, eq, egs_factory.future(days=5), input_values={"A": 1, "C": LEGACY_DATA})
    template = BookingInputTemplate.objects.create(user=owner, equipment=eq, name="T", input_values={"A": 3, "C": LEGACY_BLANK})
    before = {b.pk: copy.deepcopy(b.input_values) for b in (advanced, blank, held)}

    run(apply=False, codes=[eq.code], write=lambda *_: None)
    assert DynamicInputField.objects.get(equipment=eq, user_type="external", field_key="C").field_type == T.TABLE
    assert Booking.objects.get(pk=advanced.pk).input_values == before[advanced.pk]

    saved = []
    lines = []
    backup = run(apply=True, codes=[eq.code], write=lines.append, save_backup=saved.append)
    assert saved == [backup]
    for user_type in ("student", "faculty", "external"):
        c = DynamicInputField.objects.get(equipment=eq, user_type=user_type, field_key="C")
        assert c.field_type == T.TYPED_TABLE and c.options == [] and c.field_label == "Samples Details"
        assert [col["key"] for col in c.table_config["columns"]] == [NEW_KEY, *ROW]
        assert c.table_config["columns"][0]["required"] is True and c.table_config["columns"][0]["type"] == "TEXT"
        assert c.table_config["rows"]["initial_rows"] == 1 and c.table_config["rows"]["min_rows"] == 1
    assert ChargeProfile.objects.get(equipment=eq, user_type="student").time_formula == "A*60"

    advanced.refresh_from_db()
    assert advanced.input_values["C"] == [{NEW_KEY: "", **ROW}, {NEW_KEY: "", **ROW}]
    assert advanced.input_values[SAMPLE_SETS_KEY][0]["C"] == [{NEW_KEY: "", **ROW}]
    assert advanced.total_charge == Decimal("300.00") and advanced.status == "BOOKED"
    assert Booking.objects.get(pk=blank.pk).input_values["C"] == [{NEW_KEY: ""}, {NEW_KEY: ""}]
    assert Booking.objects.get(pk=held.pk).input_values == before[held.pk]
    template.refresh_from_db()
    assert template.input_values["C"] == [{NEW_KEY: ""}, {NEW_KEY: ""}]
    assert any("held back (plain table with data) ids: [%d]" % held.pk in line for line in lines)
    sanity = next(line for line in lines if line.startswith("  charge sanity"))
    assert "'recomputed same as before conversion': 2" in sanity and "DIFFERENT" not in sanity
    assert "'recomputed equals stored amount': 1" in sanity
    assert not [line for line in lines if "sample charge" in line and "DIFFERENT" in line]

    again = run(apply=True, codes=[eq.code], write=lambda *_: None)
    assert again["fields"] == [] and again["records"] == []

    restore(backup, apply=True, write=lambda *_: None)
    assert DynamicInputField.objects.get(equipment=eq, user_type="external", field_key="C").field_type == T.TABLE
    assert Booking.objects.get(pk=advanced.pk).input_values == before[advanced.pk]
    assert BookingInputTemplate.objects.get(pk=template.pk).input_values["C"] == LEGACY_BLANK


@pytest.mark.django_db
def test_converted_history_renders_and_needs_the_new_column_only_when_the_table_is_edited(egs_factory):
    eq = _tga(egs_factory)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, eq, egs_factory.future(), input_values={"A": 1, "C": [ROW]})
    run(apply=True, codes=[eq.code], write=lambda *_: None)
    booking.refresh_from_db()
    field = DynamicInputField.objects.get(equipment=eq, user_type="student", field_key="C")
    shown = format_input_value({"field_key": "C", "field_type": "TYPED_TABLE", "table_config": field.table_config},
                               booking.input_values)
    assert shown["columns"][:2] == ["S.No.", "Sample Name/Code"]
    assert shown["rows"] == [["1", "", "30", "800", "10", "0", "Nitrogen", "150"]]

    unchanged = _patch(egs_factory, owner, booking, {"A": 1, "C": booking.input_values["C"]})
    assert unchanged.status_code == 200, unchanged.data

    edited = _patch(egs_factory, owner, booking, {"A": 1, "C": [{**ROW, "final_temp_c": 900}]})
    assert edited.status_code == 400
    assert "Sample Name/Code is required" in edited.data["error"]

    filled = _patch(egs_factory, owner, booking, {"A": 1, "C": [{NEW_KEY: "S1", **ROW, "final_temp_c": 900}]})
    assert filled.status_code == 200, filled.data
