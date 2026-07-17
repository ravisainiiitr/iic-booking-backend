"""Time and charge calculation engines for equipment bookings."""

import math
import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Dict, List, Any, Optional, Tuple
from django.core.exceptions import ValidationError
from .models import ChargeProfile, ChargeProfilePricingProfile, ChargeProfileType, MultiParamDefinition

MONEY_QUANTIZE = Decimal("0.01")
ICPMS_STANDARDS_RUNS_PER_STANDARD = Decimal("3")
ICPMS_BLANK_SAMPLE_UNITS = Decimal("1")


def quantize_money(value: Any) -> Decimal:
    """Round monetary amounts to paise (2 dp); never truncate to whole rupees."""
    return safe_decimal(value).quantize(MONEY_QUANTIZE, rounding=ROUND_HALF_UP)


def finalize_charge_result(
    total_charge: Decimal,
    breakdown: List[Dict[str, Any]],
) -> Tuple[Decimal, List[Dict[str, Any]]]:
    """Apply total-charge approximation rules for all equipment profile types."""
    total = quantize_money(total_charge)
    finalized: List[Dict[str, Any]] = []
    for line in breakdown:
        if not isinstance(line, dict):
            finalized.append(line)
            continue
        amt = line.get("amount")
        try:
            finalized.append(
                {
                    **line,
                    "amount": float(quantize_money(amt)),
                }
            )
        except Exception:
            finalized.append(line)
    return total, finalized


def equipment_has_icpms_standard_coverage(equipment) -> bool:
    if equipment is None:
        return False
    try:
        from .models import DynamicInputFieldType

        return equipment.input_fields.filter(
            field_type=DynamicInputFieldType.ICPMS_STANDARD_COVERAGE
        ).exists()
    except Exception:
        return False


def get_icpms_standard_coverage_count(charge_profile: ChargeProfile, input_values: Dict[str, Any]) -> Decimal:
    """
    ICPMS minimum standards count stored under the coverage field's own field_key (not always literal "C").
    """
    equipment = getattr(charge_profile, "equipment", None)
    if not equipment:
        return safe_decimal(input_values.get("C", 0))
    try:
        from .models import DynamicInputFieldType

        coverage_field_keys = list(
            equipment.input_fields.filter(
                field_type=DynamicInputFieldType.ICPMS_STANDARD_COVERAGE
            ).values_list("field_key", flat=True)
        )
        if not coverage_field_keys:
            return safe_decimal(input_values.get("C", 0))

        counts = [safe_decimal(input_values.get(field_key, 0)) for field_key in coverage_field_keys]
        return max(counts) if counts else safe_decimal(input_values.get("C", 0))
    except Exception:
        return safe_decimal(input_values.get("C", 0))


def icpms_total_sample_units(num_samples: Decimal, num_standards_required: Decimal) -> Decimal:
    """
    ICPMS run-count proxy for charging: A + (3 × standards) + 1 blank.
    """
    return num_samples + (ICPMS_STANDARDS_RUNS_PER_STANDARD * num_standards_required) + ICPMS_BLANK_SAMPLE_UNITS


def safe_decimal(value: Any, default: Decimal = Decimal('0')) -> Decimal:
    """
    Safely convert a value to Decimal.
    
    Args:
        value: Value to convert (can be int, float, str, Decimal, bool, or None)
        default: Default value to return if conversion fails
    
    Returns:
        Decimal value
    """
    if value is None:
        return default
    
    if isinstance(value, Decimal):
        return value
    
    # Handle boolean values explicitly (True -> 1, False -> 0)
    if isinstance(value, bool):
        return Decimal('1') if value else Decimal('0')
    
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return default
    
    if isinstance(value, str):
        value = value.strip()
        if value == '':
            return default
        try:
            return Decimal(value)
        except (InvalidOperation, ValueError):
            return default
    
    # For other types (list, dict, etc.), try to convert to string first
    try:
        str_value = str(value).strip()
        if str_value in ['', '[]', '{}', 'None']:
            return default
        return Decimal(str_value)
    except (InvalidOperation, ValueError, TypeError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    """
    Safely convert a value to float.
    
    Args:
        value: Value to convert (can be int, float, str, bool, or None)
        default: Default value to return if conversion fails
    
    Returns:
        float value
    """
    if value is None:
        return default
    
    # Handle boolean values explicitly (True -> 1.0, False -> 0.0)
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    
    if isinstance(value, (int, float)):
        return float(value)
    
    if isinstance(value, str):
        value = value.strip()
        if value == '':
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default
    
    # For other types, try to convert to string first
    try:
        str_value = str(value).strip()
        if str_value in ['', '[]', '{}', 'None']:
            return default
        return float(str_value)
    except (ValueError, TypeError):
        return default


def parse_periodic_help_text(help_text: Optional[str]) -> tuple[set[str], set[str]]:
    """Parse PERIODIC_TABLE Help text → (disabled, preselected). See periodic_elements."""
    from .periodic_elements import parse_periodic_help_text as _parse

    return _parse(help_text)

def normalize_periodic_table_billable_counts(
    equipment,
    input_values: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    For PERIODIC_TABLE fields: ensure field count used in charge calculation excludes
    locked preselected elements defined in Help text as `/Symbol` (e.g. `/C`).
    Also ensures ``*_elements`` lists include those locked symbols for display.
    """
    if not input_values or not isinstance(input_values, dict):
        return {}
    out = dict(input_values)
    if equipment is None:
        return out

    from .models import DynamicInputField, DynamicInputFieldType

    eq_id = getattr(equipment, "pk", None) or getattr(equipment, "equipment_id", None)
    if not eq_id:
        return out

    fields = DynamicInputField.objects.filter(
        equipment_id=eq_id,
        field_type=DynamicInputFieldType.PERIODIC_TABLE,
    ).only("field_key", "help_text", "field_type")

    for field in fields:
        key = field.field_key
        if not key:
            continue
        _disabled, preselected = parse_periodic_help_text(field.help_text)
        elements_key = f"{key}_elements"
        raw_elements = out.get(elements_key, "")
        if isinstance(raw_elements, (list, tuple)):
            symbols = [str(s).strip() for s in raw_elements if str(s).strip()]
        else:
            symbols = [s.strip() for s in str(raw_elements or "").split(",") if s.strip()]

        for sym in preselected:
            if sym not in symbols:
                symbols.append(sym)

        billable = [s for s in symbols if s not in preselected]
        out[elements_key] = ",".join(symbols)
        out[key] = len(billable)

    return out


def build_safe_input_values_for_charge_calculation(
    input_values: Optional[Dict[str, Any]],
    equipment=None,
) -> Dict[str, Any]:
    """Scalar input values for time/charge; excludes periodic table ``*_elements`` keys."""
    normalized = (
        normalize_periodic_table_billable_counts(equipment, input_values)
        if equipment is not None
        else (dict(input_values) if input_values and isinstance(input_values, dict) else {})
    )
    safe: Dict[str, Any] = {}
    if not normalized:
        return safe
    for key, value in normalized.items():
        if key.endswith("_elements"):
            continue
        if isinstance(value, (int, float, bool)):
            safe[key] = value
        elif isinstance(value, str):
            value_lower = value.lower().strip()
            if value_lower in ("true", "yes"):
                safe[key] = True
            elif value_lower in ("false", "no"):
                safe[key] = False
            else:
                try:
                    safe[key] = float(value_lower) if "." in value_lower else int(value_lower)
                except (ValueError, TypeError):
                    # Keep non-numeric strings (e.g. PRINT_3D material code in field B).
                    stripped = value.strip()
                    if stripped:
                        safe[key] = stripped
    return safe


def normalize_multi_param_code(value: Any) -> Optional[str]:
    """
    Normalize MULTI_PARAM radio selection (field B) to param_code for DB lookup.

    Query params and safe_input parsing often coerce numeric codes to float/int (e.g. 1.0),
    but MultiParamDefinition.param_code is stored as a string ("1", "2", "4").
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        f = float(value)
        if f == int(f):
            return str(int(f))
        return str(value).strip()
    s = str(value).strip()
    if not s:
        return None
    try:
        f = float(s)
        if f == int(f):
            return str(int(f))
    except (ValueError, TypeError):
        pass
    return s


class TimeCalculationEngine:
    """Engine for calculating booking time based on formulas and parameters."""
    
    @staticmethod
    def calculate_time(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
        slot_duration_minutes: Optional[int] = None
    ) -> int:
        """
        Calculate total time in minutes for a booking.
        
        Args:
            charge_profile: The charge profile to use
            input_values: Dictionary of input field values (A-G)
            slot_duration_minutes: Slot duration in minutes (for formula calculations)
        
        Returns:
            Total time in minutes
        """
        import logging
        logger = logging.getLogger(__name__)
        logger.info(f"Calculating time for charge profile: {charge_profile.profile_type}")
        logger.info(f"Input values: {input_values}")
        logger.info(f"Slot duration minutes: {slot_duration_minutes}")
        if charge_profile.profile_type in [ChargeProfileType.SAMPLE, ChargeProfileType.SAMPLE_ELEMENT]:
            return TimeCalculationEngine._calculate_formula_time(
                charge_profile, input_values, slot_duration_minutes
            )
        elif charge_profile.profile_type == ChargeProfileType.HOUR:
            return TimeCalculationEngine._calculate_hour_time(
                input_values, slot_duration_minutes
            )
        elif charge_profile.profile_type == ChargeProfileType.MULTI_PARAM:
            return TimeCalculationEngine._calculate_multi_param_time(
                charge_profile, input_values
            )
        elif charge_profile.profile_type == ChargeProfileType.PRINT_3D:
            return TimeCalculationEngine._calculate_print_3d_time(input_values)
        else:
            raise ValidationError(f"Unsupported profile type: {charge_profile.profile_type}")
    
    @staticmethod
    def _calculate_multi_param_time(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
    ) -> int:
        """Calculate time for MULTI_PARAM profile type.
        
        Input B is a radio button selection (param_code).
        Finds the matching MultiParamDefinition for the user type and calculates time.
        If time_formula exists, uses it with A (number of samples) and parameter time.
        """
        # Get input B value (radio button selection - param_code)
        param_code = normalize_multi_param_code(input_values.get('B'))
        if not param_code:
            return 0
        
        # Get number of samples (A)
        a_value = input_values.get('A', 0)
        num_samples = safe_float(a_value, 0.0)
        
        # Find the matching parameter definition
        param_def = MultiParamDefinition.objects.filter(
            equipment=charge_profile.equipment,
            user_type=charge_profile.user_type,
            param_code=param_code,
            is_active=True
        ).first()
        
        if not param_def:
            return 0
        
        # Get time per sample from the parameter
        time_per_sample = param_def.unit_time_minutes
        
        # Check breakpoint flag - if 1, multiply by number of samples; if 0, don't multiply
        # Breakpoint is used as a boolean flag for MULTI_PARAM (0 or 1)
        breakpoint_flag = charge_profile.breakpoint
        if breakpoint_flag == 1:
            use_samples = True
        else:
            use_samples = False
        
        # If time_formula is provided, use it
        if charge_profile.time_formula:
            formula = charge_profile.time_formula
            
            # Replace A with number of samples
            formula = re.sub(r'(?<![a-zA-Z0-9_])A(?![a-zA-Z0-9_])', str(num_samples), formula)
            
            # Replace TIME with time_per_sample
            formula = re.sub(r'(?<![a-zA-Z0-9_])TIME(?![a-zA-Z0-9_])', str(time_per_sample), formula)
            
            try:
                calculated_time = int(eval(formula))
                return calculated_time
            except Exception as e:
                # If formula fails, fall back to default calculation
                pass
        
        # Default calculation: time_per_sample * (num_samples if breakpoint flag is enabled, else 1)
        if use_samples and num_samples > 0:
            total_time = int(time_per_sample * num_samples)
        else:
            total_time = time_per_sample
        
        return total_time
    
    @staticmethod
    def _calculate_hour_time(
        input_values: Dict[str, Any],
        slot_duration_minutes: int,
    ) -> int:
        """Calculate time for HOUR profile type.
        
        Time = input B value * slot_duration_minutes
        """
        # Get input B value (number of slots)
        b_value = input_values.get('B', 0)
        num_slots = safe_float(b_value, 0.0)
        
        return int(num_slots * slot_duration_minutes)
    
    
    @staticmethod
    def _calculate_print_3d_time(input_values: Dict[str, Any]) -> int:
        """PRINT_3D: time in minutes from field C (STL analysis result)."""
        return max(0, int(safe_float(input_values.get("C", 0), 0.0)))
    
    @staticmethod
    def _calculate_formula_time(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
        slot_duration_minutes: Optional[int]
    ) -> int:
        """Calculate time using formula for SAMPLE or SAMPLE_ELEMENT profiles."""
        if not charge_profile.time_formula:
            # If no formula, default to slot duration
            return slot_duration_minutes or 0
        
        formula = charge_profile.time_formula

        equipment = getattr(charge_profile, "equipment", None)
        has_icpms = equipment_has_icpms_standard_coverage(equipment)
        icpms_standards = get_icpms_standard_coverage_count(charge_profile, input_values) if has_icpms else Decimal("0")

        # Prepare variable values - use word boundaries to match only standalone variable names
        variable_values = {}
        
        for key in ['A', 'B', 'C', 'D', 'E', 'F', 'G']:
            if key == 'C':
                variable_values[key] = float(icpms_standards if has_icpms else safe_decimal(input_values.get("C", 0)))
            elif key in input_values:
                value = input_values[key]
                numeric_value = safe_float(value, 0.0)
                variable_values[key] = numeric_value
            elif key == 'B' and slot_duration_minutes:
                variable_values[key] = float(slot_duration_minutes)
            else:
                variable_values[key] = 0

        if has_icpms:
            num_samples = safe_decimal(input_values.get("A", 0))
            variable_values["S"] = float(icpms_total_sample_units(num_samples, icpms_standards))
        
        # Replace variables in formula using regex
        # For single-letter variables (A-G), we need to match them as standalone identifiers
        # Pattern: match variable that is not part of a longer identifier
        # This means: start of string OR non-word char, then the variable, then end of string OR non-word char
        for key, value in variable_values.items():
            # Pattern matches: (start or non-word-char) + key + (end or non-word-char)
            # This prevents matching 'A' inside 'AB' or 'ABC'
            pattern = r'(?<![a-zA-Z0-9_])' + re.escape(key) + r'(?![a-zA-Z0-9_])'
            formula = re.sub(pattern, str(value), formula)

        try:
            # Evaluate the formula safely
            # Note: In production, consider using a safer expression evaluator like 'simpleeval'
            result = eval(formula)
            return int(result)
        except Exception as e:
            raise ValidationError(f"Error evaluating time formula '{charge_profile.time_formula}': {str(e)}")


class ChargeCalculationEngine:
    """Engine for calculating booking charges based on charge profiles."""
    
    @staticmethod
    def calculate_charge(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
        total_time_minutes: int,
        selected_parameters: Optional[List[str]] = None
    ) -> Tuple[Decimal, List[Dict[str, Any]]]:
        """
        Calculate total charge and breakdown for a booking.
        
        Args:
            charge_profile: The charge profile to use
            input_values: Dictionary of input field values (A-G)
            total_time_minutes: Total time in minutes
            selected_parameters: List of selected parameter codes (for MULTI_PARAM)
        
        Returns:
            Tuple of (total_charge, charge_breakdown)
            charge_breakdown is a list of dicts with 'description' and 'amount'
        """
        # Discounted Charge Profile is defined as a full waiver for charges:
        # we still compute time (slots/booking duration) but return ₹0 charges.
        if getattr(charge_profile, "pricing_profile", None) == ChargeProfilePricingProfile.DISCOUNTED:
            return finalize_charge_result(
                Decimal("0.00"),
                [{"description": "Discounted Charge Profile", "amount": 0.0}],
            )

        if charge_profile.profile_type == ChargeProfileType.SAMPLE:
            total, breakdown = ChargeCalculationEngine._calculate_sample_charge(
                charge_profile, input_values, total_time_minutes
            )
        elif charge_profile.profile_type == ChargeProfileType.HOUR:
            total, breakdown = ChargeCalculationEngine._calculate_hour_charge(
                charge_profile, input_values, total_time_minutes
            )
        elif charge_profile.profile_type == ChargeProfileType.SAMPLE_ELEMENT:
            total, breakdown = ChargeCalculationEngine._calculate_sample_element_charge(
                charge_profile, input_values
            )
        elif charge_profile.profile_type == ChargeProfileType.MULTI_PARAM:
            total, breakdown = ChargeCalculationEngine._calculate_multi_param_charge(
                charge_profile, input_values, total_time_minutes
            )
        elif charge_profile.profile_type == ChargeProfileType.PRINT_3D:
            total, breakdown = ChargeCalculationEngine._calculate_print_3d_charge(
                charge_profile, input_values, total_time_minutes
            )
        else:
            raise ValidationError(f"Unsupported profile type: {charge_profile.profile_type}")

        return finalize_charge_result(total, breakdown)
    
    @staticmethod
    def _calculate_sample_charge(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
        total_time_minutes: int
    ) -> Tuple[Decimal, List[Dict[str, Any]]]:
        """Calculate charge for SAMPLE profile type."""
        breakdown = []
        total_charge = Decimal('0.00')
        
        num_samples = safe_decimal(input_values.get('A', 0))
        
        if num_samples <= 0:
            return Decimal('0.00'), breakdown
            
        # Check if breakpoint applies (compare per-sample value: total / A)
        # Breakpoint is treated as a threshold for (total_time_minutes / num_samples).
        breakpoint_value = (
            safe_decimal(charge_profile.breakpoint)
            if charge_profile.breakpoint is not None
            else Decimal('0')
        )
        per_sample_minutes = safe_decimal(total_time_minutes) / num_samples
        if breakpoint_value > 0 and per_sample_minutes > breakpoint_value:
            # Use secondary unit charge
            unit_charge = charge_profile.secondary_unit_charge or Decimal('0.00')
            breakdown.append({
                'description': f'{num_samples} samples @ {unit_charge} per sample',
                'amount': float(num_samples * unit_charge)
            })
            total_charge = num_samples * unit_charge * (Decimal(str(total_time_minutes)) / Decimal('60'))
        else:
            # Use primary unit charge
            unit_charge = charge_profile.primary_unit_charge or Decimal('0.00')
            breakdown.append({
                'description': f'{num_samples} samples @ {unit_charge} per sample',
                'amount': float(num_samples * unit_charge)
            })
            total_charge = num_samples * unit_charge
        
        return total_charge, breakdown
    
    @staticmethod
    def _calculate_hour_charge(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
        total_time_minutes: int
    ) -> Tuple[Decimal, List[Dict[str, Any]]]:
        """Calculate charge for HOUR profile type.
        
        Inputs:
            A: Number of samples
            B: Number of slots
            C: Toggle (enabled/disabled)
        
        Charge Calculation:
            Base: (Time per slot / 60) * primary_unit_charge
            If toggle (C) enabled: Base + secondary_unit_charge
        """
        breakdown = []
        total_charge = Decimal('0.00')
        
        # Get slot duration from equipment
        slot_duration_minutes = charge_profile.equipment.slot_duration_minutes if charge_profile.equipment else 0
        
        if slot_duration_minutes <= 0:
            return Decimal('0.00'), breakdown
        
        # Get toggle value (C) - check if enabled
        toggle_value = input_values.get('C', 0)
        try:
            # Toggle is enabled if C is truthy (1, '1', True, 'true', etc.)
            toggle_enabled = bool(toggle_value) and str(toggle_value).lower() not in ['0', 'false', 'no', '']
        except (ValueError, TypeError):
            toggle_enabled = False
        
        # Get number of slots (B)
        b_value = input_values.get('B', 0)
        num_slots = safe_decimal(b_value)
        
        if num_slots <= 0:
            return Decimal('0.00'), breakdown
        
        # Calculate time per slot in hours
        time_per_slot_hours = safe_decimal(slot_duration_minutes) / Decimal('60')
        
        # Base charge per slot: (Time per slot / 60) * primary_unit_charge
        charge_per_slot = time_per_slot_hours * charge_profile.primary_unit_charge
        
        # Total base charge for all slots
        base_charge = charge_per_slot * num_slots
        breakdown.append({
            'description': f'{num_slots} slot(s) × {time_per_slot_hours:.2f} hours @ {charge_profile.primary_unit_charge} per hour',
            'amount': float(base_charge)
        })
        total_charge = base_charge
        
        # Add secondary unit charge if toggle is enabled
        if toggle_enabled and charge_profile.secondary_unit_charge:
            total_charge += charge_profile.secondary_unit_charge
            breakdown.append({
                'description': 'Additional charge (toggle enabled)',
                'amount': float(charge_profile.secondary_unit_charge)
            })
        
        return total_charge, breakdown
    
    @staticmethod
    def _calculate_sample_element_charge(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any]
    ) -> Tuple[Decimal, List[Dict[str, Any]]]:
        """Calculate charge for SAMPLE_ELEMENT profile type."""
        breakdown = []
        total_charge = Decimal('0.00')

        # Get samples (typically field A) and elements (typically field B)
        num_samples = safe_decimal(input_values.get('A', 0))
        num_elements = safe_decimal(input_values.get('B', 0))
        num_standards_required = safe_decimal(input_values.get('C', 0))
        breakpoint = safe_decimal(charge_profile.breakpoint) if charge_profile.breakpoint is not None else Decimal('0')
        
        if num_samples <= 0:
            return Decimal('0.00'), breakdown

        equipment = getattr(charge_profile, "equipment", None)
        has_icpms_coverage = equipment_has_icpms_standard_coverage(equipment)
        if has_icpms_coverage:
            num_standards_required = get_icpms_standard_coverage_count(charge_profile, input_values)

        # If equipment uses ICPMS Standard Coverage, apply the special charge model:
        # Let `S = A + (3 × C) + 1 (blank)` where:
        # - A = samples
        # - B = element count
        # - C = auto-calculated standards required (from ICPMS_STANDARD_COVERAGE input)
        # - element limit = breakpoint
        standards_factor = (
            icpms_total_sample_units(num_samples, num_standards_required)
            if has_icpms_coverage
            else num_samples + (Decimal("3") * num_standards_required)
        )

        if has_icpms_coverage:
            base_charge = standards_factor * charge_profile.primary_unit_charge

            def _format_nos_label(value: Any) -> str:
                """Whole numbers without trailing .00 for display (e.g. breakpoint / extra element counts)."""
                d = safe_decimal(value, Decimal("0"))
                if d == d.to_integral_value():
                    return str(int(d))
                return format(d.normalize(), "f").rstrip("0").rstrip(".")

            breakpoint_nos = _format_nos_label(breakpoint) if breakpoint is not None else ""
            standards_factor_label = _format_nos_label(standards_factor)

            breakdown.append({
                "description": (
                    "Total samples = Number of samples + (Number of standards × 3) + 1 (blank) "
                    f"= {standards_factor_label}\n"
                    "* Factor of 3 since three concentrations per standard needs to be run\n"
                    f"Base charge = Total samples × {charge_profile.primary_unit_charge}"
                ),
                "amount": float(base_charge),
            })
            total_charge = base_charge

            if num_elements >= breakpoint and breakpoint is not None:
                extra_elements = num_elements - breakpoint
                if extra_elements > 0 and charge_profile.secondary_unit_charge:
                    element_charge = extra_elements * charge_profile.secondary_unit_charge * standards_factor
                    breakdown.append({
                        "description": (
                            f"Charges for extra elements beyond {breakpoint_nos} nos: "
                            f"({_format_nos_label(extra_elements)}) × {charge_profile.secondary_unit_charge} × Total Samples({standards_factor_label})"
                        ),
                        "amount": float(element_charge),
                    })
                    total_charge += element_charge

            return total_charge, breakdown

        # Default SAMPLE_ELEMENT charge model (legacy):
        # - primary: A × primary_unit_charge
        # - secondary: max(0, B - breakpoint) × secondary_unit_charge × A
        base_charge = num_samples * charge_profile.primary_unit_charge
        breakdown.append({
            'description': f'{num_samples} samples @ {charge_profile.primary_unit_charge} per sample',
            'amount': float(base_charge)
        })
        total_charge = base_charge
        
        if num_elements > breakpoint:
            extra_elements = num_elements - breakpoint
            element_charge = extra_elements * charge_profile.secondary_unit_charge * num_samples
            breakdown.append({
                'description': f'{extra_elements} extra elements × {num_samples} samples @ {charge_profile.secondary_unit_charge} per element/sample',
                'amount': float(element_charge)
            })
            total_charge += element_charge
        
        return total_charge, breakdown
    
    @staticmethod
    def _calculate_multi_param_charge(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
        total_time_minutes: int
    ) -> Tuple[Decimal, List[Dict[str, Any]]]:
        """Calculate charge for MULTI_PARAM profile type.
        
        Input B is a radio button selection (param_code).
        Finds the matching MultiParamDefinition for the user type and calculates charge.
        If breakpoint flag is enabled, multiplies by number of samples (A).
        """
        breakdown = []
        total_charge = Decimal('0.00')
        
        # Get input B value (radio button selection - param_code)
        param_code = normalize_multi_param_code(input_values.get('B'))
        if not param_code:
            return Decimal('0.00'), breakdown
        
        # Get number of samples (A)
        a_value = input_values.get('A', 0)
        num_samples = safe_decimal(a_value)
        
        # Find the matching parameter definition
        param_def = MultiParamDefinition.objects.filter(
            equipment=charge_profile.equipment,
            user_type=charge_profile.user_type,
            param_code=param_code,
            is_active=True
        ).first()
        
        if not param_def:
            return Decimal('0.00'), breakdown
        
        # Get charge per sample from the parameter
        charge_per_sample = param_def.unit_charge
        
        # Check breakpoint flag - if 1, multiply by number of samples; if 0, don't multiply
        # Breakpoint is used as a boolean flag for MULTI_PARAM (0 or 1)
        breakpoint_flag = charge_profile.breakpoint
        if breakpoint_flag == 1:
            use_samples = True
        else:
            use_samples = False
        
        # Calculate charge
        if use_samples and num_samples > 0:
            # If breakpoint flag is true: No of sample * Charge per sample
            total_charge = num_samples * charge_per_sample
            breakdown.append({
                'description': f'{num_samples} samples × {param_def.param_name} @ {charge_per_sample} per sample',
                'amount': float(total_charge)
            })
        else:
            # If breakpoint flag is false: Charge per sample (based on radio input field)
            total_charge = charge_per_sample
            breakdown.append({
                'description': f'{param_def.param_name} @ {charge_per_sample}',
                'amount': float(total_charge)
            })
        
        return total_charge, breakdown

    @staticmethod
    def _calculate_print_3d_charge(
        charge_profile: ChargeProfile,
        input_values: Dict[str, Any],
        total_time_minutes: int,
    ) -> Tuple[Decimal, List[Dict[str, Any]]]:
        """PRINT_3D: material cost (weight × price/gram) + machine time cost."""
        from .models import PrintMaterial

        breakdown: List[Dict[str, Any]] = []
        weight_g = safe_decimal(input_values.get("A", 0))
        if weight_g > 0:
            weight_g = Decimal(int(math.ceil(float(weight_g))))
        material_code = str(input_values.get("B", "") or "").strip()
        if weight_g <= 0 or not material_code:
            return Decimal("0.00"), breakdown

        material_qs = PrintMaterial.objects.filter(
            equipment=charge_profile.equipment,
            code=material_code,
            is_active=True,
        )
        user_type = getattr(charge_profile, "user_type", None)
        material = (
            material_qs.filter(user_type=user_type).first()
            or material_qs.filter(user_type__isnull=True).first()
            or material_qs.first()
        )
        if not material:
            raise ValidationError(f"Unknown or inactive print material: {material_code}")

        material_cost = weight_g * material.price_per_gram
        breakdown.append({
            "description": f"{weight_g} g × {material.name} @ {material.price_per_gram}/g",
            "amount": float(material_cost),
        })
        total_charge = material_cost

        hourly_rate = charge_profile.primary_unit_charge or Decimal("0.00")
        if hourly_rate > 0 and total_time_minutes > 0:
            machine_cost = (safe_decimal(total_time_minutes) / Decimal("60")) * hourly_rate
            breakdown.append({
                "description": f"{total_time_minutes} min machine time @ {hourly_rate}/hour",
                "amount": float(machine_cost),
            })
            total_charge += machine_cost

        return total_charge, breakdown

