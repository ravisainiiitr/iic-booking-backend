"""One equipment's rate card, shared by the Analysis Charges catalog and the public equipment page.

Only STANDARD, active charge profiles are included. ``visible`` decides which user categories the caller may see.
"""

from __future__ import annotations

from collections.abc import Callable

from django.db.models import Prefetch

from .models import ChargeProfile
from .models import ChargeProfilePricingProfile
from .models import DynamicInputField
from .models import MultiParamDefinition


def rate_card_prefetches() -> list[Prefetch]:
    return [
        Prefetch(
            "charge_profiles",
            queryset=ChargeProfile.objects.filter(
                pricing_profile=ChargeProfilePricingProfile.STANDARD,
                is_active=True,
            ).order_by("user_type"),
            to_attr="analysis_charge_profiles",
        ),
        Prefetch(
            "input_fields",
            queryset=DynamicInputField.objects.filter(field_key="B").order_by("user_type", "field_key"),
            to_attr="analysis_input_fields_b",
        ),
        Prefetch(
            "param_definitions",
            queryset=MultiParamDefinition.objects.filter(is_active=True).order_by("user_type", "param_code"),
            to_attr="analysis_param_definitions",
        ),
    ]


def rate_card(eq, visible: Callable[[str], bool]) -> dict | None:
    """Rates for the categories ``visible`` allows, or None when there are none. Needs ``rate_card_prefetches``."""
    profiles = [cp for cp in (getattr(eq, "analysis_charge_profiles", None) or []) if visible(cp.user_type)]
    if not profiles:
        return None
    return {
        "charge_profiles": [
            {
                "user_type": cp.user_type,
                "profile_type": cp.effective_profile_type,
                "is_active": cp.is_active,
                "primary_unit_charge": str(cp.primary_unit_charge) if cp.primary_unit_charge is not None else None,
                "secondary_unit_charge": str(cp.secondary_unit_charge) if cp.secondary_unit_charge is not None else None,
                "breakpoint": str(cp.breakpoint) if cp.breakpoint is not None else None,
                "display_text": cp.display_text or "",
            }
            for cp in profiles
        ],
        "input_fields": [
            {
                "field_key": f.field_key,
                "field_label": f.field_label,
                "options": f.options or [],
                "user_type": f.user_type or "",
            }
            for f in getattr(eq, "analysis_input_fields_b", None) or []
            if visible(f.user_type)
        ],
        "slot_options": [
            {
                "user_type": p.user_type,
                "param_name": p.param_name,
                "param_code": p.param_code,
                "unit_charge": str(p.unit_charge) if p.unit_charge is not None else None,
                "unit_time_minutes": p.unit_time_minutes,
                "display_text": getattr(p, "display_text", None) or "",
                "is_active": p.is_active,
            }
            for p in getattr(eq, "analysis_param_definitions", None) or []
            if visible(p.user_type)
        ],
    }
