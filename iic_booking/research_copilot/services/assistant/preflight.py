"""
Pre-proposal checks for an in-chat booking, using the same services the book endpoint calls.

Run before the summary is shown so users see a blocking rule (portal freeze, department block,
equipment status, I-STEM confirmation, slot length, quota, wallet balance, supervisor spending limit)
before they press Confirm instead of after. The confirm click still goes through `_book_equipment_impl`,
which repeats every check under row locks, so nothing here is the last line of defence.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


def _clean_like_book(raw: dict[str, Any]) -> dict[str, Any]:
    """The book endpoint's input cleaning: numeric strings become numbers, yes/no become booleans."""
    out: dict[str, Any] = {}
    for key, value in (raw or {}).items():
        if value in ("", [], None):
            continue
        if isinstance(value, str):
            low = value.strip().lower()
            if not low:
                continue
            try:
                out[key] = float(low) if "." in low else int(low)
            except ValueError:
                if low in ("true", "yes"):
                    out[key] = True
                elif low in ("false", "no"):
                    out[key] = False
                else:
                    out[key] = value.strip()
        elif isinstance(value, bool):
            if value:
                out[key] = value
        else:
            out[key] = value
    return out


def exact_charge(user, eq, input_values: dict[str, Any], slots: list) -> tuple[dict[str, Any] | None, str | None]:
    """What the book endpoint will charge for these inputs and slots (charge engine on booked slot minutes + GST)."""
    from iic_booking.equipment.api_views import _get_charge_profile_pricing_profile_for_user, get_external_gst_percent
    from iic_booking.equipment.calculators import (
        ChargeCalculationEngine,
        build_safe_input_values_for_charge_calculation,
        normalize_periodic_table_billable_counts,
        quantize_money,
    )
    from iic_booking.equipment.models import ChargeProfile
    from iic_booking.equipment.pi_pricing import get_active_charge_profile
    from iic_booking.research_copilot.services.v2.mutations.booking import _slot_minutes
    from iic_booking.users.models.user_type import UserType

    user_type = getattr(user, "user_type", None) or UserType.STUDENT
    try:
        profile = get_active_charge_profile(eq, user_type, _get_charge_profile_pricing_profile_for_user(user, eq), user)
    except ChargeProfile.DoesNotExist:
        return None, f"No active charge profile found for equipment {eq.pk} and user type {user_type}."
    values = normalize_periodic_table_billable_counts(eq, _clean_like_book(input_values))
    minutes = sum(_slot_minutes(s) for s in slots)
    try:
        safe = build_safe_input_values_for_charge_calculation(values, equipment=eq)
        charge, breakdown = ChargeCalculationEngine.calculate_charge(profile, safe, minutes, selected_parameters=None)
    except Exception as exc:  # noqa: BLE001
        return None, f"Error calculating charge: {exc}"
    charge = quantize_money(Decimal(str(charge)))
    total = charge
    pct = Decimal("0")
    gst = Decimal("0.00")
    if UserType.is_external_user(user_type):
        pct = Decimal(str(get_external_gst_percent() or 0))
        if pct > 0:
            gst = quantize_money(charge * pct / Decimal("100"))
            total = quantize_money(charge + gst)
    show = bool(getattr(profile, "show_charge_breakdown", True))
    return {
        "charge": float(charge),
        "gst_percent": float(pct),
        "gst_amount": float(gst),
        "total": float(total),
        "slot_minutes": minutes,
        "breakdown": [
            {"label": str(row.get("description") or row.get("label") or ""), "amount": float(row.get("amount") or 0)}
            for row in (breakdown or [])
            if isinstance(row, dict)
        ][:8] if show else [],
    }, None


def _wallet(user, eq, total: Decimal | None) -> dict[str, Any]:
    from iic_booking.equipment.booking_payment_service import compute_booking_payment_split
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    out: dict[str, Any] = {"label": "No wallet linked", "balance": None, "error": None, "amount_due": None, "target": None}
    try:
        target, _ = WalletRepository.get_booking_wallet_target(user, getattr(eq, "internal_department", None))
    except Exception:  # noqa: BLE001
        target = None
    if target is None:
        out["error"] = "You don't have access to any wallet."
        return out
    out["target"] = target
    wallet = getattr(target, "wallet", None) or target
    owner = getattr(wallet, "user", None)
    dept = getattr(target, "department", None)
    if owner is not None and int(getattr(owner, "pk", 0) or 0) != int(user.pk):
        out["label"] = f"{getattr(owner, 'name', '') or getattr(owner, 'email', 'Supervisor')}'s wallet"
    else:
        out["label"] = "Your wallet"
    if dept is not None:
        out["label"] += f" ({getattr(dept, 'code', '') or getattr(dept, 'name', '')})"
    for attr in ("balance", "total_balance"):
        try:
            value = getattr(target, attr)
            if value is not None:
                out["balance"] = float(value)
                break
        except Exception:  # noqa: BLE001
            continue
    if total is not None:
        try:
            _applied, due, err = compute_booking_payment_split(
                target, total, user_type=getattr(user, "user_type", "") or "", create_as_hold=False
            )
            out["error"] = err
            out["amount_due"] = float(due) if due else 0.0
        except Exception:  # noqa: BLE001
            pass
    return out


def _spending(user, target, total: Decimal | None) -> tuple[dict[str, Any] | None, str | None]:
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.student_spending_limits import limit_summary, spending_limit_error, supervisor_link_for

    if getattr(user, "user_type", None) in UserType.get_admin_panel_codes():
        return None, None
    try:
        link = supervisor_link_for(user, target)
    except Exception:  # noqa: BLE001
        return None, None
    if link is None or not link.spending_limit_enabled:
        return None, None
    summary = limit_summary(link)
    err = spending_limit_error(user, target, total) if total is not None else None
    return {
        "weekly_limit": summary.get("weekly_limit_inr"),
        "weekly_remaining": summary.get("weekly_remaining_inr"),
        "monthly_limit": summary.get("monthly_limit_inr"),
        "monthly_remaining": summary.get("monthly_remaining_inr"),
    }, err


def _quota_error(user, eq, minutes: int, total: Decimal | None, start) -> str | None:
    from iic_booking.equipment.quota_utils import QuotaService, booking_quota_should_skip
    from iic_booking.users.models.user_type import UserType

    if getattr(user, "user_type", None) in UserType.get_admin_panel_codes() or booking_quota_should_skip(eq):
        return None
    try:
        allowed, err = QuotaService.validate_booking_quota(
            user=user,
            equipment=eq,
            additional_time_minutes=minutes,
            additional_bookings=1,
            additional_charge=total if total is not None else Decimal("0"),
            booking_date=start,
            bypass_quota=False,
        )
    except Exception:  # noqa: BLE001
        return None
    return None if allowed else str(err or "This booking would exceed your booking quota.")


def _slot_limit_error(user, eq, slots: list) -> str | None:
    from iic_booking.equipment.equipment_slot_quota import slot_limit_error

    try:
        return slot_limit_error(
            user, eq, slots_requested=len(slots), reference=slots[0].start_datetime if slots else None
        )
    except Exception:  # noqa: BLE001
        return None


def account_problems(user, eq) -> list[str]:
    """Rules that block booking this equipment at all, before any slot is picked."""
    from iic_booking.equipment.api_views import user_can_see_equipment
    from iic_booking.equipment.models import EquipmentStatus
    from iic_booking.research_copilot.services.v2.mutations.booking import istem_ack_error
    from iic_booking.users.legacy_ledger.booking_lock import booking_is_locked, department_equipment_booking_blocked

    out: list[str] = []
    locked, message = booking_is_locked(user)
    if locked:
        out.append(message or "Booking is temporarily locked.")
    if not user_can_see_equipment(user, eq):
        out.append("This equipment is not available to your account.")
    blocked, dept_message = department_equipment_booking_blocked(eq, user)
    if blocked:
        out.append(dept_message or "Booking is disabled for this department.")
    if (eq.status or "").strip() != EquipmentStatus.ACTIVE:
        out.append(f"Booking is not allowed while equipment is {eq.get_status_display()}.")
    istem = istem_ack_error(user)
    if istem:
        out.append(istem)
    return out


def run(user, eq, input_values: dict[str, Any], slots: list) -> dict[str, Any]:
    """All pre-confirm checks for one proposed booking. `problems` non-empty means do not propose."""
    problems = account_problems(user, eq)
    charge, charge_error = exact_charge(user, eq, input_values, slots)
    if charge_error:
        problems.append(charge_error)
    total = Decimal(str(charge["total"])) if charge else None
    wallet = _wallet(user, eq, total)
    if wallet.get("error"):
        problems.append(str(wallet["error"]))
    spending, spending_error = _spending(user, wallet.get("target"), total) if wallet.get("target") is not None else (None, None)
    if spending_error:
        problems.append(spending_error)
    minutes = int(charge["slot_minutes"]) if charge else 0
    quota_error = _quota_error(user, eq, minutes, total, slots[0].start_datetime if slots else None)
    if quota_error:
        problems.append(quota_error)
    slot_limit_error = _slot_limit_error(user, eq, slots)
    if slot_limit_error:
        problems.append(slot_limit_error)
    after = None
    if wallet.get("balance") is not None and total is not None:
        after = float(Decimal(str(wallet["balance"])) - total)
    wallet.pop("target", None)
    return {
        "ok": not problems,
        "problems": problems,
        "charge": charge,
        "wallet": wallet,
        "balance_after": after,
        "spending": spending,
    }
