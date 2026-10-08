""""from ₹X" starting prices for catalog cards, for the viewer's own user type."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Min, Q

from .charge_visibility import is_internal_rate_user_type, viewer_may_see_internal_rates
from .models import ChargeProfile, ChargeProfilePricingProfile, EquipmentProfileType, MultiParamDefinition

# GENERIC profiles price through a free-form formula, so the primary charge has no known unit.
UNIT_BY_PROFILE_TYPE = {
    EquipmentProfileType.SAMPLE: "sample",
    EquipmentProfileType.SAMPLE_ELEMENT: "sample",
    EquipmentProfileType.MULTI_PARAM: "sample",
    EquipmentProfileType.HOUR: "hour",
    EquipmentProfileType.PRINT_3D: "hour",
    EquipmentProfileType.LASER_CUT_2D: "hour",
}


def _viewer_user_type(user):
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    user_type = str(getattr(user, "user_type", "") or "").strip()
    if not user_type:
        return None
    # Discounted accounts are not charged the standard rate; showing it would mislead.
    if getattr(user, "use_discounted_charge_profile", False):
        return None
    if is_internal_rate_user_type(user_type) and not viewer_may_see_internal_rates(user):
        return None
    return user_type


def catalog_from_prices(user, equipment_ids) -> dict[int, dict]:
    """
    {equipment_id: {"from_price": "1500.00", "from_price_unit": "hour"}} for equipment with an
    active charge profile for the viewer's user type and a positive, unit-bearing rate. Where the
    viewer is billed at an Equipment PI's rates (see pi_pricing), the PI profile is used.
    """
    from iic_booking.users.models import UserType

    from .pi_pricing import pi_rate_user_types_by_equipment

    user_type = _viewer_user_type(user)
    ids = [i for i in equipment_ids if i is not None]
    if not user_type or not ids:
        return {}

    pi_types = pi_rate_user_types_by_equipment(user, ids)
    rows = ChargeProfile.objects.filter(equipment_id__in=ids, is_active=True).filter(
        Q(user_type=user_type, pricing_profile=ChargeProfilePricingProfile.STANDARD)
        | Q(equipment_id__in=list(pi_types), pricing_profile=ChargeProfilePricingProfile.PI)
    ).values_list(
        "equipment_id", "user_type", "pricing_profile", "profile_type", "equipment__profile_type", "primary_unit_charge"
    )
    standard = {}
    pi_rows = {}
    for equipment_id, row_user_type, pricing, profile_type, equipment_profile_type, primary in rows:
        value = (row_user_type, profile_type or equipment_profile_type, primary)
        if pricing == ChargeProfilePricingProfile.PI:
            pi_rows.setdefault(equipment_id, {})[row_user_type] = value
        else:
            standard[equipment_id] = value

    applied = {}
    for equipment_id in ids:
        if equipment_id in pi_types:
            by_type = pi_rows.get(equipment_id, {})
            for candidate in (user_type, pi_types[equipment_id], UserType.FACULTY):
                if candidate in by_type:
                    applied[equipment_id] = by_type[candidate]
                    break
        if equipment_id not in applied and equipment_id in standard:
            applied[equipment_id] = standard[equipment_id]

    multi_param_types = {
        row_user_type for row_user_type, effective, _p in applied.values()
        if effective == EquipmentProfileType.MULTI_PARAM
    }
    multi_param_min = {}
    if multi_param_types:
        multi_param_min = {
            (equipment_id, row_user_type): m
            for equipment_id, row_user_type, m in MultiParamDefinition.objects.filter(
                equipment_id__in=ids, user_type__in=multi_param_types, is_active=True, unit_charge__gt=0
            )
            .order_by()
            .values("equipment_id", "user_type")
            .annotate(m=Min("unit_charge"))
            .values_list("equipment_id", "user_type", "m")
        }

    out: dict[int, dict] = {}
    for equipment_id, (row_user_type, effective, primary) in applied.items():
        unit = UNIT_BY_PROFILE_TYPE.get(effective)
        if unit is None:
            continue
        price = (
            multi_param_min.get((equipment_id, row_user_type))
            if effective == EquipmentProfileType.MULTI_PARAM
            else primary
        )
        if price is None or Decimal(price) <= 0:
            continue
        out[equipment_id] = {"from_price": f"{Decimal(price):.2f}", "from_price_unit": unit}
    return out
