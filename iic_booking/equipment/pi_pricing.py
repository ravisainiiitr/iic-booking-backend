"""Server-side Equipment PI pricing resolution.

Frontend must not submit is_pi / spoofed amounts; booking and estimate always
resolve the ChargeProfile pricing_profile via resolve_pricing_profile_for_user.

Precedence for a booking user on an equipment:
1. PI rates, when the user or the owner of the wallet they book against (an approved
   wallet join for students) is an active Equipment PI of that equipment and the
   equipment has an active PI ChargeProfile. The PI row for the user's own type wins,
   else the PI's type ("PI IIT Faculty"), else faculty.
2. Discounted (waiver) for users flagged use_discounted_charge_profile.
3. Standard rates for the user's own type.
"""

from __future__ import annotations

from django.core.cache import cache

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


def category_estimate_pricing_profile(viewer, equipment, user_type) -> str:
    """
    Pricing profile for a "Calculate charges" estimate of the category ``user_type``.

    A signed-in user estimating their own category gets the profile their booking will use (PI rates
    for students on an Equipment PI's wallet, waiver), so the estimate matches the charge. Any other
    category is estimated at the standard rate.
    """
    if viewer is None or not getattr(viewer, "is_authenticated", False):
        return ChargeProfilePricingProfile.STANDARD
    own = str(getattr(viewer, "user_type", "") or "").strip().casefold()
    if own and own == str(user_type or "").strip().casefold():
        return resolve_pricing_profile_for_user(viewer, equipment)
    return ChargeProfilePricingProfile.STANDARD


PI_FACULTY_CACHE_KEY = "pi_pricing:pi_faculty_by_equipment:v1"
# Display-only readers (catalog prices) use this map; charges always read the database.
PI_FACULTY_CACHE_SECONDS = 60


def invalidate_pi_faculty_cache() -> None:
    cache.delete(PI_FACULTY_CACHE_KEY)


def pi_faculty_by_equipment() -> dict:
    """{equipment_id: {faculty_id, ...}} of active Equipment PIs on equipment with active PI rates."""
    cached = cache.get(PI_FACULTY_CACHE_KEY)
    if cached is None:
        with_rates = ChargeProfile.objects.filter(
            pricing_profile=ChargeProfilePricingProfile.PI, is_active=True
        ).values("equipment_id")
        cached = {}
        for equipment_id, faculty_id in EquipmentPI.objects.filter(
            is_active=True, equipment_id__in=with_rates
        ).values_list("equipment_id", "faculty_id"):
            cached.setdefault(equipment_id, set()).add(faculty_id)
        cache.set(PI_FACULTY_CACHE_KEY, cached, PI_FACULTY_CACHE_SECONDS)
    return cached


def pi_rate_user_types_by_equipment(user, equipment_ids) -> dict:
    """
    {equipment_id: user type of the Equipment PI whose PI rates apply} for the equipment in
    ``equipment_ids`` where ``resolve_pricing_profile_for_user`` gives PI for this user.
    """
    if not user or getattr(user, "pk", None) is None or not equipment_ids:
        return {}
    pi_map = pi_faculty_by_equipment()
    relevant = {eq_id: pi_map[eq_id] for eq_id in equipment_ids if eq_id in pi_map}
    out = {eq_id: user.user_type for eq_id, faculty_ids in relevant.items() if user.pk in faculty_ids}
    remaining = [eq_id for eq_id in relevant if eq_id not in out]
    if remaining:
        owner = wallet_owner_user(user)
        if owner is not None and owner.pk != user.pk:
            for eq_id in remaining:
                if owner.pk in relevant[eq_id]:
                    out[eq_id] = owner.user_type
    return out


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
