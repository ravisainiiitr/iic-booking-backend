"""Min / Max / Step / Max formula of NUMERIC input fields: one resolver (options first, then the help-text
convention), validation when the equipment form saves them, and the combined A/B limit using them."""

from __future__ import annotations

import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.calculators import SAMPLE_SETS_KEY
from iic_booking.equipment.models import DynamicInputField, DynamicInputFieldType, Equipment
from iic_booking.equipment.numeric_field_limits import (
    is_numeric_help_text_convention,
    normalize_numeric_field_config,
    numeric_constraints,
    numeric_help_text_for_display,
    numeric_max_formula,
    resolve_numeric_field_bounds,
)
from iic_booking.equipment.sample_set_limits import combined_limit_fields, combined_max_error, configured_static_max
from iic_booking.equipment.serializers import DynamicInputFieldWriteSerializer
from iic_booking.equipment.tests.test_equipment_create_paths import _admin, _dept, _portal_payload

# --- resolver ---------------------------------------------------------------------------------------


def test_options_take_precedence_over_help_text():
    c = numeric_constraints(options={"min": 2, "max": 8, "step": 0.5}, help_text="1\n2\n1")
    assert (c["min"], c["max"], c["step"]) == (2, 8, 0.5)
    assert c["source"] == {"min": "options", "max": "options", "step": "options"}


def test_help_text_lines_fill_what_options_do_not_set():
    c = numeric_constraints(options={"max": 4}, help_text="1\n2\n0.5")
    assert (c["min"], c["max"], c["step"]) == (1, 4, 0.5)
    assert c["source"] == {"min": "help_text", "max": "options", "step": "help_text"}


def test_nothing_configured_gives_none_not_ui_defaults():
    c = numeric_constraints(options=[], help_text="")
    assert (c["min"], c["max"], c["step"], c["max_formula"]) == (None, None, None, "")
    assert resolve_numeric_field_bounds(options=[], help_text="") == (0.0, 100.0, 1.0)


def test_non_positive_option_step_falls_back_to_help_text():
    assert numeric_constraints(options={"step": 0}, help_text="0\n10\n0.1")["step"] == 0.1


def test_max_formula_from_dict_and_legacy_values():
    assert numeric_max_formula({"min": 1, "max_formula": " B*4 "}) == "B*4"
    assert numeric_max_formula("B*4") == "B*4"
    assert numeric_max_formula(["B*4"]) == "B*4"
    assert numeric_max_formula(["a", "b"]) == ""
    assert numeric_max_formula({"max": 3}) == ""


def test_bounds_unchanged_for_existing_shapes():
    assert resolve_numeric_field_bounds(options=[], help_text="1\n2\n1") == (1.0, 2.0, 1.0)
    assert resolve_numeric_field_bounds(options={"min": 1, "max": 4}, help_text="") == (1.0, 4.0, 1.0)
    assert resolve_numeric_field_bounds(options={"min": 1, "max_formula": "B*4"}, help_text="", formula_max=8) == (
        1.0, 8.0, 1.0,
    )


@pytest.mark.parametrize(
    "help_text,pure",
    [
        ("1\n2\n1", True),
        ("1\r\n2\r\n0.01", True),
        ("0\n\n0.5", True),
        ("0 100 0.01", True),
        ("Enter the count\nMax 10", False),
        ("1\n2\n1\nslots of 90 minutes", False),
        ("Number of hours", False),
        ("", False),
    ],
)
def test_help_text_convention_detection(help_text, pure):
    assert is_numeric_help_text_convention(help_text) is pure


def test_convention_help_text_is_not_shown_to_users():
    assert numeric_help_text_for_display("NUMERIC", "1\n2\n1") == ""
    assert numeric_help_text_for_display("NUMERIC", "Max 2 slots per booking") == "Max 2 slots per booking"
    assert numeric_help_text_for_display("TEXT", "1\n2\n1") == "1\n2\n1"


# --- normalising on save ----------------------------------------------------------------------------


def test_pure_help_text_is_folded_into_options_and_cleared():
    options, help_text = normalize_numeric_field_config([], "1\n2\n1")
    assert options == {"min": 1, "max": 2, "step": 1}
    assert help_text == ""


def test_options_win_when_folding_help_text():
    options, help_text = normalize_numeric_field_config({"max": "3"}, "1\n2\n1")
    assert options == {"min": 1, "max": 3, "step": 1}
    assert help_text == ""


def test_descriptive_help_text_is_kept():
    options, help_text = normalize_numeric_field_config({"min": 1, "max": 2}, "Each slot is 1.5 hours")
    assert options == {"min": 1, "max": 2}
    assert help_text == "Each slot is 1.5 hours"


def test_blank_values_are_dropped_and_formula_kept():
    options, _ = normalize_numeric_field_config({"min": "1", "max": "", "step": None, "max_formula": " B*4 "}, "")
    assert options == {"min": 1, "max_formula": "B*4"}
    assert normalize_numeric_field_config({"min": "", "max_formula": ""}, "") == ([], "")


def test_legacy_formula_options_are_left_alone():
    assert normalize_numeric_field_config("B*4", "1\n2\n1") == ("B*4", "1\n2\n1")


@pytest.mark.parametrize(
    "options,message",
    [
        ({"min": 5, "max": 2}, "Min (5) cannot be greater than Max (2)."),
        ({"step": 0}, "Step must be greater than 0."),
        ({"step": -1}, "Step must be greater than 0."),
        ({"max": "two"}, "Max must be a number."),
        ({"min": 0.5, "step": 1}, "Min must be a whole number when Step is a whole number."),
        ({"max_formula": "B*four"}, "Max formula may only use"),
        ({"max_formula": "b*4"}, "Max formula may only use"),
        ({"max_formula": "B**"}, "Max formula is not a valid expression"),
    ],
)
def test_invalid_values_are_rejected(options, message):
    with pytest.raises(ValueError, match=message.replace("(", r"\(").replace(")", r"\)").replace("*", r"\*")):
        normalize_numeric_field_config(options, "")


def test_decimal_step_allows_decimal_limits():
    assert normalize_numeric_field_config({"min": 0.5, "max": 2.5, "step": 0.5}, "")[0] == {
        "min": 0.5, "max": 2.5, "step": 0.5,
    }
    assert normalize_numeric_field_config({"max_formula": "SLOT_DURATION_MINUTES/30 + A"}, "")[0] == {
        "max_formula": "SLOT_DURATION_MINUTES/30 + A",
    }


def _write(**overrides):
    data = {
        "user_type": "student", "field_key": "B", "field_label": "No. of Slots", "field_type": "NUMERIC",
        "options": {}, "help_text": "",
    }
    data.update(overrides)
    return DynamicInputFieldWriteSerializer(data=data)


def test_write_serializer_validates_numeric_options():
    bad = _write(options={"min": 3, "max": 2})
    assert not bad.is_valid()
    assert bad.errors["options"] == ["Field B (student): Min (3) cannot be greater than Max (2)."]

    ok = _write(options={"min": "1", "max": "2", "step": "1"}, help_text="1\n2\n1")
    assert ok.is_valid(), ok.errors
    assert ok.validated_data["options"] == {"min": 1, "max": 2, "step": 1}
    assert ok.validated_data["help_text"] == ""


def test_write_serializer_ignores_non_numeric_fields():
    s = _write(field_type="RADIO", options=["x", "y"], help_text="1\n2\n1")
    assert s.is_valid(), s.errors
    assert s.validated_data["options"] == ["x", "y"]
    assert s.validated_data["help_text"] == "1\n2\n1"


# --- equipment form API → combined A/B limit --------------------------------------------------------


def _apreo_inputs(b_options, b_help_text=""):
    return [
        {"user_type": "student", "field_key": "A", "field_label": "No. of Samples", "field_type": "NUMERIC",
         "is_required": True, "default_value": "1", "options": {"min": 1, "max_formula": "B*4"}, "help_text": ""},
        {"user_type": "student", "field_key": "B", "field_label": "Number of Slots", "field_type": "NUMERIC",
         "is_required": True, "default_value": "1", "options": b_options, "help_text": b_help_text},
    ]


@pytest.mark.django_db
def test_b_max_set_through_the_equipment_form_is_the_combined_limit():
    client = APIClient()
    client.force_authenticate(user=_admin())
    code = f"NUM{uuid.uuid4().hex[:5].upper()}"
    payload = _portal_payload(code, _dept())
    payload["input_fields"] = _apreo_inputs({"min": 1, "max": 2, "step": 1})

    res = client.post("/api/admin/equipment/", payload, format="json")
    assert res.status_code == 201, getattr(res, "data", res.content[:2000])

    eq = Equipment.objects.get(code=code)
    b = DynamicInputField.objects.get(equipment=eq, user_type="student", field_key="B")
    assert b.options == {"min": 1, "max": 2, "step": 1}
    assert configured_static_max(b) == 2
    assert combined_limit_fields(eq, user_type="student") == [("B", "Number of Slots", 2)]
    assert combined_max_error(eq, {"A": 2, "B": 1, SAMPLE_SETS_KEY: [{"A": 2, "B": 1}]}, user_type="student") is None
    assert combined_max_error(eq, {"A": 2, "B": 2, SAMPLE_SETS_KEY: [{"A": 2, "B": 2}]}, user_type="student") == (
        "Total Number of Slots across all sample sets (4) exceeds the maximum allowed (2) for this equipment."
    )


@pytest.mark.django_db
def test_saving_help_text_only_field_moves_limits_to_options_without_changing_them():
    client = APIClient()
    client.force_authenticate(user=_admin())
    code = f"NUM{uuid.uuid4().hex[:5].upper()}"
    payload = _portal_payload(code, _dept())
    payload["input_fields"] = []
    assert client.post("/api/admin/equipment/", payload, format="json").status_code == 201
    eq = Equipment.objects.get(code=code)
    DynamicInputField.objects.create(
        equipment=eq, user_type="student", field_key="B", field_label="Number of Slots",
        field_type=DynamicInputFieldType.NUMERIC, options=[], help_text="1\n2\n1",
    )
    before = combined_limit_fields(eq, user_type="student")

    res = client.patch(
        f"/api/admin/equipment/{eq.pk}/", {"input_fields": _apreo_inputs([], "1\n2\n1")}, format="json"
    )
    assert res.status_code == 200, getattr(res, "data", res.content[:2000])

    b = DynamicInputField.objects.get(equipment=eq, user_type="student", field_key="B")
    assert (b.options, b.help_text) == ({"min": 1, "max": 2, "step": 1}, "")
    assert combined_limit_fields(eq, user_type="student") == before == [("B", "Number of Slots", 2)]


@pytest.mark.django_db
def test_equipment_form_rejects_min_above_max():
    client = APIClient()
    client.force_authenticate(user=_admin())
    code = f"NUM{uuid.uuid4().hex[:5].upper()}"
    payload = _portal_payload(code, _dept())
    payload["input_fields"] = _apreo_inputs({"min": 3, "max": 2})

    res = client.post("/api/admin/equipment/", payload, format="json")

    assert res.status_code == 400
    assert "Min (3) cannot be greater than Max (2)." in str(res.data)
    assert not Equipment.objects.filter(code=code).exists()
