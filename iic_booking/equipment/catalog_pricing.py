""""from ₹X" starting prices for catalog cards, for the viewer's own user type."""

from __future__ import annotations

from decimal import Decimal

from django.db.models import Min

from .charge_visibility import is_internal_rate_user_type, viewer_may_see_internal_rates
from .models import ChargeProfile, ChargeProfilePricingProfile, EquipmentProfileType, MultiParamDefinition

# GENERIC profiles price through a free-form formula, so the primary charge has no known unit.
UNIT_BY_PROFILE_TYPE = {
    EquipmentProfileType.SAMPLE: "sample",
    EquipmentProfileType.SAMPLE_ELEMENT: "sample",
    EquipmentProfileType.MULTI_PARAM: "sample",
    EquipmentProfileType.HOUR: "hour",
    EquipmentProfileType.PRINT_3D: "hour",
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
    active STANDARD charge profile for the viewer's user type and a positive, unit-bearing rate.
    Two queries regardless of catalog size.
    """
    user_type = _viewer_user_type(user)
    ids = [i for i in equipment_ids if i is not None]
    if not user_type or not ids:
        return {}

    profiles = ChargeProfile.objects.filter(
        equipment_id__in=ids,
        user_type=user_type,
        pricing_profile=ChargeProfilePricingProfile.STANDARD,
        is_active=True,
    ).values_list("equipment_id", "profile_type", "equipment__profile_type", "primary_unit_charge")

    multi_param_min = dict(
        MultiParamDefinition.objects.filter(
            equipment_id__in=ids, user_type=user_type, is_active=True, unit_charge__gt=0
        )
        .order_by()
        .values("equipment_id")
        .annotate(m=Min("unit_charge"))
        .values_list("equipment_id", "m")
    )

    out: dict[int, dict] = {}
    for equipment_id, profile_type, equipment_profile_type, primary in profiles:
        effective = profile_type or equipment_profile_type
        unit = UNIT_BY_PROFILE_TYPE.get(effective)
        if unit is None:
            continue
        price = multi_param_min.get(equipment_id) if effective == EquipmentProfileType.MULTI_PARAM else primary
        if price is None or Decimal(price) <= 0:
            continue
        out[equipment_id] = {"from_price": f"{Decimal(price):.2f}", "from_price_unit": unit}
    return out
