"""Who may see internal (IIT Roorkee student / faculty) charge rates, and revenue on reports."""

from __future__ import annotations

from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

BOOKING_STATS_MONEY_KEYS = ("total_spent", "average_cost", "refunded_amount")
EQUIPMENT_REPORT_SUMMARY_MONEY_KEYS = ("revenue_total", "revenue_internal", "revenue_external")
BOOKING_MONEY_KEYS = (
    "total_charge",
    "wallet_amount_applied",
    "amount_due",
    "amount_paid",
    "charge_breakdown",
    "charge_recalculation_pending_amount",
    "return_shipping_fee_amount",
    "own_material_fixed_charge",
)
FABRICATION_ITEM_MONEY_KEYS = ("price_per_gram_snapshot", "estimated_material_cost")


def internal_rate_user_types() -> set[str]:
    return {str(c).lower() for c in UserType.get_internal_user_codes()}


def is_internal_rate_user_type(code) -> bool:
    return bool(code) and str(code).strip().lower() in internal_rate_user_types()


def viewer_may_see_internal_rates(user) -> bool:
    """Internal IITR users and staff see internal rates; anonymous and external users do not."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    user_type = str(getattr(user, "user_type", "") or "")
    if user_type in UserType.get_management_user_codes():
        return True
    if not is_internal_rate_user_type(user_type):
        return False
    department = getattr(user, "department", None)
    if department is not None and getattr(department, "department_type", None) == DepartmentType.EXTERNAL:
        return False
    return True


def request_may_see_internal_rates(request) -> bool:
    return viewer_may_see_internal_rates(getattr(request, "user", None) if request is not None else None)


def viewer_may_see_report_revenue(user) -> bool:
    """Reports & Statistics money figures (charged, revenue, refunds). Lab Operators see usage only."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    return str(getattr(user, "user_type", "") or "") != UserType.OPERATOR


def strip_booking_stats_money(data: dict) -> dict:
    for key in BOOKING_STATS_MONEY_KEYS:
        data.pop(key, None)
    return data


def viewer_may_see_booking_money(user, booking) -> bool:
    """Lab Operators see a booking's charges only when it is their own booking."""
    if user is None or str(getattr(user, "user_type", "") or "") != UserType.OPERATOR:
        return True
    return getattr(booking, "user_id", None) == getattr(user, "pk", None)


def strip_booking_money(data: dict) -> dict:
    for key in BOOKING_MONEY_KEYS:
        data.pop(key, None)
    for key in ("print_analyses", "laser_cut_analyses"):
        for item in data.get(key) or []:
            _strip_fabrication_item_money(item)
    for key in ("print_analysis", "print_analysis_batch"):
        nested = data.get(key)
        if isinstance(nested, dict):
            _strip_fabrication_item_money(nested)
            for item in nested.get("items") or []:
                _strip_fabrication_item_money(item)
    return data


def _strip_fabrication_item_money(item) -> None:
    if isinstance(item, dict):
        for key in FABRICATION_ITEM_MONEY_KEYS:
            item.pop(key, None)
