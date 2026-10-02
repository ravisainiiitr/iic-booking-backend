"""Equipment limits that apply to all sample sets of one booking taken together.

Field A (usually the number of samples) and field B (usually slots / hours / elements) can carry
a configured maximum. With "Samples with different parameters" a booking holds several sample
sets, and the configured maximum is the limit for the booking as a whole: the sum of the field
over sample set 1 and every additional set must not exceed it.
"""

from __future__ import annotations

from typing import Any, Optional

from .calculators import split_sample_sets
from .numeric_field_limits import _to_float, numeric_constraints, numeric_max_formula

COMBINED_LIMIT_FIELD_KEYS = ("A", "B")


def _has_max_formula(options: Any) -> bool:
    return bool(numeric_max_formula(options))


def configured_static_max(field) -> Optional[float]:
    """The maximum set on the equipment for a NUMERIC field (options.max, else help_text line 2).

    The UI fallback of 100 is not an equipment rule, so it is not returned. A formula maximum
    (e.g. A <= B*4) is a per-set relationship and is checked per set only.
    """
    configured = numeric_constraints(options=field.options, help_text=field.help_text)
    if configured["max_formula"]:
        return None
    return configured["max"]


def booking_field_user_type(booking) -> str:
    """User type whose input fields a booking was made with (same order as BookingSerializer.input_fields)."""
    return (
        (getattr(booking, "user_type_snapshot", None) or "").strip()
        or (getattr(getattr(booking, "charge_profile", None), "user_type", None) or "").strip()
        or (getattr(getattr(booking, "user", None), "user_type", None) or "").strip()
    )


def combined_limit_fields(equipment, booking_user=None, user_type=None) -> list[tuple[str, str, float]]:
    """[(field_key, label, max)] for fields A/B of ``equipment`` that have a configured maximum.

    Uses the booking form's resolution: rows typed for the user type (``user_type``, else the booking
    user's type), else shared rows.
    """
    from .models import DynamicInputField, DynamicInputFieldType

    qs = DynamicInputField.objects.filter(
        equipment=equipment,
        field_key__in=COMBINED_LIMIT_FIELD_KEYS,
        field_type=DynamicInputFieldType.NUMERIC,
    ).only("field_key", "field_label", "options", "help_text", "user_type")
    user_type = str(user_type or getattr(booking_user, "user_type", "") or "")
    rows = list(qs.filter(user_type=user_type)) if user_type else []
    if not rows:
        rows = list(qs.filter(user_type=""))
    out = []
    for field in sorted(rows, key=lambda f: f.field_key):
        max_v = configured_static_max(field)
        if max_v is not None:
            out.append((field.field_key, field.field_label or field.field_key, max_v))
    return out


def sample_set_count(input_values) -> int:
    """Number of additional sample sets (sample set 1 is the booking's own inputs)."""
    _base, sets = split_sample_sets(input_values if isinstance(input_values, dict) else {})
    return len(sets)


SAMPLE_SETS_DISABLED_MESSAGE = (
    "This equipment does not accept samples with different parameters. Book all samples with the same "
    "parameters, or make a separate booking for samples that need different settings."
)


def sample_sets_allowed(equipment) -> bool:
    """The equipment's "Allow samples with different parameters" switch (on unless turned off)."""
    return getattr(equipment, "allow_multiple_sample_sets", True) is not False


def sample_sets_disabled_error(equipment, input_values, baseline=None) -> Optional[str]:
    """Error when the switch is off and ``input_values`` hold more extra sample sets than ``baseline``.

    Bookings and templates saved before the switch was turned off keep their sets: those may be edited
    or removed, but no set may be added.
    """
    if sample_sets_allowed(equipment):
        return None
    if sample_set_count(input_values) > sample_set_count(baseline):
        return SAMPLE_SETS_DISABLED_MESSAGE
    return None


def combined_total(input_values, key: str) -> float:
    base, sets = split_sample_sets(input_values if isinstance(input_values, dict) else {})
    total = 0.0
    for group in (base, *sets):
        value = _to_float(group.get(key))
        if value is not None:
            total += value
    return total


def _pretty(value: float):
    return int(value) if float(value).is_integer() else round(value, 6)


def combined_max_error(
    equipment, input_values, *, booking_user=None, user_type=None, baseline=None
) -> Optional[str]:
    """Error message when a field A/B total across all sample sets exceeds its configured maximum.

    Only bookings with additional sample sets are checked (a single set is covered by the per-field
    check). ``baseline`` is the booking's stored inputs when editing: a total that already exceeded
    the maximum before this rule existed may be kept or lowered, but not raised.
    """
    if not sample_set_count(input_values):
        return None
    for key, label, max_v in combined_limit_fields(equipment, booking_user, user_type):
        total = combined_total(input_values, key)
        if total <= max_v:
            continue
        if baseline is not None and total <= combined_total(baseline, key):
            continue
        return (
            f"Total {label} across all sample sets ({_pretty(total)}) exceeds the maximum allowed "
            f"({_pretty(max_v)}) for this equipment."
        )
    return None
