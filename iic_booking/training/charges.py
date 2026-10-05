"""
Demonstration charges at the equipment's internal IITR rate.

The rate is what the requesting faculty member's own bookings on that equipment cost per hour of instrument
time: the same charge profile a booking would use (``resolve_pricing_profile_for_user`` /
``get_active_charge_profile``, falling back to the standard internal Faculty profile), run through the booking
charge engine. Hour-based profiles give their hourly rate directly; other profiles are converted from one
unit (one sample, one parameter run) to an hourly equivalent using the engine's own time for that unit.

The amount is rate × duration rounded to whole rupees like booking charges, deducted from the wallet the
faculty member's bookings use (``WalletRepository.get_booking_wallet_target``: the sub-wallet for the
equipment's internal department) with the same balance rules as bookings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from django.db import DatabaseError, transaction

from .models import DemoPurpose

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


@dataclass
class InternalRate:
    rate_per_hour: Decimal | None
    basis: str
    user_type_label: str = ""


def course_demos_free() -> bool:
    """Main Admin switch in Training Policy (default off: course demonstrations are charged too)."""
    from .models import TrainingModuleSettings

    try:
        with transaction.atomic():
            value = (
                TrainingModuleSettings.objects.filter(pk=TrainingModuleSettings.SINGLETON_PK)
                .values_list("course_demos_free", flat=True)
                .first()
            )
    except DatabaseError:
        logger.warning("course_demos_free unavailable; treating course demonstrations as chargeable", exc_info=True)
        return False
    return bool(value)


def is_chargeable(purpose: str) -> bool:
    return not (purpose == DemoPurpose.COURSE and course_demos_free())


def amount_for(rate: Decimal, minutes: int) -> Decimal:
    from iic_booking.equipment.calculators import quantize_money

    if not rate or rate <= 0 or not minutes:
        return ZERO
    return quantize_money(Decimal(rate) * Decimal(int(minutes)) / Decimal(60)).quantize(Decimal("0.01"))


def _internal_profile(equipment, user):
    from iic_booking.equipment.models import ChargeProfile, ChargeProfilePricingProfile
    from iic_booking.equipment.pi_pricing import get_active_charge_profile, resolve_pricing_profile_for_user
    from iic_booking.users.models.user_type import UserType

    user_type = getattr(user, "user_type", None) or UserType.FACULTY
    try:
        pricing = resolve_pricing_profile_for_user(user, equipment)
    except Exception:
        pricing = ChargeProfilePricingProfile.STANDARD
    for ut, pp, who in (
        (user_type, pricing, user),
        (user_type, ChargeProfilePricingProfile.STANDARD, None),
        (UserType.FACULTY, ChargeProfilePricingProfile.STANDARD, None),
    ):
        try:
            return get_active_charge_profile(equipment, ut, pp, who)
        except ChargeProfile.DoesNotExist:
            continue
    return None


def _unit_inputs(profile, equipment) -> dict | None:
    from iic_booking.equipment.calculators import get_charge_profile_type
    from iic_booking.equipment.models import ChargeProfileType, MultiParamDefinition

    ptype = get_charge_profile_type(profile)
    if ptype in (ChargeProfileType.PRINT_3D, ChargeProfileType.LASER_CUT_2D):
        return None
    if ptype == ChargeProfileType.MULTI_PARAM:
        code = (
            MultiParamDefinition.objects.filter(equipment=equipment, user_type=profile.user_type, is_active=True)
            .order_by("param_code")
            .values_list("param_code", flat=True)
            .first()
        )
        return {"A": 1, "B": code} if code else None
    return {"A": 1}


def internal_rate(equipment, user) -> InternalRate:
    """Hourly internal IITR rate for ``user`` on ``equipment``; ``rate_per_hour`` is None when none applies."""
    from iic_booking.equipment.calculators import (
        ChargeCalculationEngine,
        TimeCalculationEngine,
        build_safe_input_values_for_charge_calculation,
        get_charge_profile_type,
    )
    from iic_booking.equipment.models import ChargeProfilePricingProfile, ChargeProfileType
    from iic_booking.users.models.user_type import UserType

    profile = _internal_profile(equipment, user)
    if profile is None:
        return InternalRate(None, "No internal IITR charge is configured for this equipment.")
    label = str(dict(UserType.get_choices()).get(profile.user_type, profile.user_type))
    if profile.pricing_profile == ChargeProfilePricingProfile.DISCOUNTED:
        return InternalRate(ZERO, f"Discounted internal rate ({label}): no charge", label)
    if get_charge_profile_type(profile) == ChargeProfileType.HOUR:
        rate = Decimal(profile.primary_unit_charge or 0).quantize(Decimal("0.01"))
        return InternalRate(rate, f"Internal IITR rate ({label}): per hour", label)
    inputs = _unit_inputs(profile, equipment)
    if inputs is None:
        return InternalRate(None, "This equipment's internal charge cannot be converted to an hourly rate.", label)
    try:
        safe = build_safe_input_values_for_charge_calculation(inputs, equipment=equipment)
        minutes = int(
            TimeCalculationEngine.calculate_time(profile, safe, slot_duration_minutes=equipment.slot_duration_minutes) or 0
        )
        charge, _ = ChargeCalculationEngine.calculate_charge(profile, safe, minutes, selected_parameters=None)
    except Exception:
        logger.warning("demo internal rate failed equipment=%s profile=%s", equipment.pk, profile.pk, exc_info=True)
        return InternalRate(None, "This equipment's internal charge cannot be converted to an hourly rate.", label)
    if minutes <= 0:
        return InternalRate(None, "This equipment's internal charge cannot be converted to an hourly rate.", label)
    rate = (Decimal(charge) * Decimal(60) / Decimal(minutes)).quantize(Decimal("0.01"))
    return InternalRate(rate, f"Internal IITR rate ({label}): ₹{Decimal(charge):,.2f} per {minutes} min of instrument time", label)


def find_wallet(user, equipment):
    """Sub-wallet the user's bookings on this equipment use, without creating one (None when missing)."""
    from iic_booking.users.models.wallet import SubWallet

    wallet = user.get_accessible_wallet()
    if wallet is None:
        return None
    department = equipment.internal_department
    if department is None:
        return SubWallet.objects.filter(wallet=wallet, department__name="General").select_related("department").first()
    return SubWallet.objects.filter(wallet=wallet, department=department).select_related("department").first()


def wallet_label(equipment) -> str:
    department = getattr(equipment, "internal_department", None)
    return f"{department.name} sub-wallet" if department is not None else "General sub-wallet"


def balance_problem(user, equipment, amount: Decimal) -> str | None:
    """Same check as bookings (``subwallet_booking_balance_ok``); None when the amount can be deducted."""
    from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

    if not amount or amount <= 0:
        return None
    if user.get_accessible_wallet() is None:
        return "You do not have a wallet to pay for the demonstration. Please set up your wallet first."
    sub = find_wallet(user, equipment)
    if sub is None:
        return f"Insufficient wallet balance in your {wallet_label(equipment)}. Required: ₹{amount:.2f}, Available: ₹0.00"
    ok, err = subwallet_booking_balance_ok(sub, amount, False)
    if ok:
        return None
    return f"{err} ({wallet_label(equipment)})" if err else "Insufficient wallet balance."


def quote(equipment, user, *, purpose: str, minutes: int) -> dict:
    """Estimate shown on the request form; same numbers the OIC's approval will charge."""
    sub = find_wallet(user, equipment)
    base = {
        "purpose": purpose,
        "minutes": int(minutes or 0),
        "course_demos_free": course_demos_free(),
        "wallet_label": wallet_label(equipment),
        "wallet_balance": str(sub.balance) if sub is not None else ("0.00" if user.get_accessible_wallet() else None),
    }
    if not is_chargeable(purpose):
        return {**base, "chargeable": False, "rate_available": True, "rate_per_hour": "0.00", "amount": "0.00",
                "basis": "Course/curricular demonstrations are free.", "balance_error": None}
    rate = internal_rate(equipment, user)
    if rate.rate_per_hour is None:
        return {**base, "chargeable": True, "rate_available": False, "rate_per_hour": None, "amount": None,
                "basis": rate.basis, "balance_error": None}
    amount = amount_for(rate.rate_per_hour, minutes)
    return {
        **base,
        "chargeable": amount > 0,
        "rate_available": True,
        "rate_per_hour": str(rate.rate_per_hour),
        "amount": str(amount),
        "basis": rate.basis,
        "balance_error": balance_problem(user, equipment, amount),
    }


def debit(req, *, description: str):
    """Deduct ``req.charge_amount`` from the requester's booking wallet; returns (sub_wallet, txn)."""
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok, subwallet_minimum_balance_after_debit

    from .errors import TrainingError

    target, _ = WalletRepository.get_booking_wallet_target(req.requester, req.equipment.internal_department)
    if target is None:
        raise TrainingError(
            "The faculty member has no wallet to pay for this demonstration.", code="no_wallet"
        )
    ok, err = subwallet_booking_balance_ok(target, req.charge_amount, False)
    if not ok:
        raise TrainingError(
            f"The faculty member's {wallet_label(req.equipment)} cannot cover the demonstration charge: {err}",
            code="insufficient_balance",
        )
    try:
        txn = target.debit(
            req.charge_amount,
            description=description,
            related_user=req.requester,
            minimum_balance_after=subwallet_minimum_balance_after_debit(target),
        )
    except ValueError:
        raise TrainingError(
            f"Insufficient balance in the faculty member's {wallet_label(req.equipment)} (₹{req.charge_amount} needed).",
            code="insufficient_balance",
        ) from None
    return target, txn
