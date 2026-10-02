"""Server-side Equipment PI pricing resolution.

Frontend must not submit is_pi / spoofed amounts; booking and estimate always
resolve the ChargeProfile pricing_profile via resolve_pricing_profile_for_user.
"""

from __future__ import annotations

from .models import ChargeProfile, ChargeProfilePricingProfile, EquipmentPI, UserDiscountedChargeEquipment
from .request_memo import memo_get_or_compute


def _wallet_owner_user_uncached(user):
    try:
        wallet = user.get_accessible_wallet()
    except Exception:
        wallet = None
    if wallet is None:
        return None
    return getattr(wallet, "user", None)


def wallet_owner_user(user):
    """Return the wallet owner User for billing identity, or None."""
    if not user:
        return None
    user_id = getattr(user, "pk", None)
    if user_id is None:
        return _wallet_owner_user_uncached(user)
    return memo_get_or_compute(
        ("pi_pricing.wallet_owner_user", user_id),
        lambda: _wallet_owner_user_uncached(user),
    )


def is_equipment_pi(user, equipment) -> bool:
    """True if user is an active EquipmentPI for equipment."""
    if not user or not equipment:
        return False
    user_id = getattr(user, "pk", None) or getattr(user, "id", None)
    equipment_id = getattr(equipment, "pk", None) or getattr(equipment, "equipment_id", None)
    if not user_id or not equipment_id:
        return False
    return memo_get_or_compute(
        ("pi_pricing.is_equipment_pi", user_id, equipment_id),
        lambda: EquipmentPI.objects.filter(
            equipment_id=equipment_id,
            faculty_id=user_id,
            is_active=True,
        ).exists(),
    )


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
    return memo_get_or_compute(
        ("pi_pricing.equipment_has_pi_charge_profiles", equipment_id),
        lambda: ChargeProfile.objects.filter(
            equipment_id=equipment_id,
            pricing_profile=ChargeProfilePricingProfile.PI,
            is_active=True,
        ).exists(),
    )


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


def pi_rate_user_type(user, equipment):
    """User type of the Equipment PI whose PI rates apply to this billing identity, or None."""
    if not user:
        return None
    if is_equipment_pi(user, equipment):
        return getattr(user, "user_type", None)
    owner = wallet_owner_user(user)
    if owner is not None and getattr(owner, "pk", None) != getattr(user, "pk", None):
        if is_equipment_pi(owner, equipment):
            return getattr(owner, "user_type", None)
    return None


def get_active_charge_profile(equipment, user_type, pricing_profile, booking_user=None):
    """
    Active ChargeProfile for (equipment, user_type, pricing_profile).

    PI rates are configured for the PI's own category ("PI IIT Faculty"). Students and
    other members billed through an Equipment PI's wallet get those rates unless a PI row
    exists for their own user type. Raises ChargeProfile.DoesNotExist when none applies.
    """
    rows = ChargeProfile.objects.filter(
        equipment=equipment, pricing_profile=pricing_profile, is_active=True
    )
    own = rows.filter(user_type=user_type).first()
    if own is not None:
        return own
    if pricing_profile == ChargeProfilePricingProfile.PI:
        from iic_booking.users.models import UserType

        for pi_type in (pi_rate_user_type(booking_user, equipment), UserType.FACULTY):
            if pi_type and pi_type != user_type:
                row = rows.filter(user_type=pi_type).first()
                if row is not None:
                    return row
    raise ChargeProfile.DoesNotExist(
        f"No active {pricing_profile} charge profile applies to user type {user_type}."
    )


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
