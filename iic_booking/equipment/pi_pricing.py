"""Server-side Equipment PI pricing resolution.

Frontend must not submit is_pi / spoofed amounts; booking and estimate always
resolve the ChargeProfile pricing_profile via resolve_pricing_profile_for_user.
"""

from __future__ import annotations

from .models import ChargeProfile, ChargeProfilePricingProfile, EquipmentPI, UserDiscountedChargeEquipment


def wallet_owner_user(user):
    """Return the wallet owner User for billing identity, or None."""
    if not user:
        return None
    try:
        wallet = user.get_accessible_wallet()
    except Exception:
        wallet = None
    if wallet is None:
        return None
    return getattr(wallet, "user", None)


def is_equipment_pi(user, equipment) -> bool:
    """True if user is an active EquipmentPI for equipment."""
    if not user or not equipment:
        return False
    user_id = getattr(user, "pk", None) or getattr(user, "id", None)
    equipment_id = getattr(equipment, "pk", None) or getattr(equipment, "equipment_id", None)
    if not user_id or not equipment_id:
        return False
    return EquipmentPI.objects.filter(
        equipment_id=equipment_id,
        faculty_id=user_id,
        is_active=True,
    ).exists()


def billing_identity_is_equipment_pi(user, equipment) -> bool:
    """
    True if the booking user OR their wallet owner is an active Equipment PI.
    """
    if is_equipment_pi(user, equipment):
        return True
    owner = wallet_owner_user(user)
    if owner is None:
        return False
    owner_id = getattr(owner, "pk", None)
    user_id = getattr(user, "pk", None)
    if owner_id is not None and owner_id == user_id:
        return False
    return is_equipment_pi(owner, equipment)


def equipment_has_pi_charge_profiles(equipment) -> bool:
    """True if equipment has at least one active PI ChargeProfile."""
    if not equipment:
        return False
    equipment_id = getattr(equipment, "pk", None) or getattr(equipment, "equipment_id", None)
    if not equipment_id:
        return False
    return ChargeProfile.objects.filter(
        equipment_id=equipment_id,
        pricing_profile=ChargeProfilePricingProfile.PI,
        is_active=True,
    ).exists()


def standard_or_discounted_pricing_profile(user, equipment) -> str:
    """
    Existing STANDARD / DISCOUNTED resolution using UserDiscountedChargeEquipment.

    - use_discounted_charge_profile False => STANDARD
    - True with no override rows => DISCOUNTED for all equipment
    - True with override rows => DISCOUNTED only for overridden equipment
    """
    if not user:
        return ChargeProfilePricingProfile.STANDARD
    if not bool(getattr(user, "use_discounted_charge_profile", False)):
        return ChargeProfilePricingProfile.STANDARD
    if equipment is None:
        return ChargeProfilePricingProfile.DISCOUNTED

    overrides_exist = UserDiscountedChargeEquipment.objects.filter(
        user=user, is_active=True
    ).exists()
    if not overrides_exist:
        return ChargeProfilePricingProfile.DISCOUNTED

    overridden = UserDiscountedChargeEquipment.objects.filter(
        user=user, equipment=equipment, is_active=True
    ).exists()
    return (
        ChargeProfilePricingProfile.DISCOUNTED
        if overridden
        else ChargeProfilePricingProfile.STANDARD
    )


def resolve_pricing_profile_for_user(user, equipment) -> str:
    """
    Resolve ChargeProfilePricingProfile code for a user+equipment.

    PI first when billing identity is an Equipment PI AND PI profiles exist;
    otherwise STANDARD / DISCOUNTED.
    """
    if billing_identity_is_equipment_pi(user, equipment) and equipment_has_pi_charge_profiles(
        equipment
    ):
        return ChargeProfilePricingProfile.PI
    return standard_or_discounted_pricing_profile(user, equipment)


def pricing_resolution_meta(user, equipment) -> dict:
    """Explain which billing identity / PI flags produced the resolved profile."""
    owner = wallet_owner_user(user) if user else None
    current_is_pi = is_equipment_pi(user, equipment) if user else False
    owner_is_pi = is_equipment_pi(owner, equipment) if owner else False
    has_pi = equipment_has_pi_charge_profiles(equipment)
    billing_is_pi = billing_identity_is_equipment_pi(user, equipment) if user else False
    resolved = (
        resolve_pricing_profile_for_user(user, equipment)
        if user
        else ChargeProfilePricingProfile.STANDARD
    )
    return {
        "billing_identity_is_pi": billing_is_pi,
        "current_user_is_pi": current_is_pi,
        "wallet_owner_is_pi": owner_is_pi,
        "equipment_has_pi_profiles": has_pi,
        "wallet_owner_id": getattr(owner, "pk", None) if owner else None,
        "wallet_owner_email": getattr(owner, "email", None) if owner else None,
        "resolved_pricing_profile": resolved,
    }
