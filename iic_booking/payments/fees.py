"""Convenience fee helpers for Razorpay checkout."""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

from iic_booking.equipment.models import BookingChargeSetting

FEE_PERCENT_KEY = "RAZORPAY_CONVENIENCE_FEE_PERCENT"
FEE_GST_PERCENT_KEY = "RAZORPAY_CONVENIENCE_FEE_GST_PERCENT"
DEFAULT_FEE_PERCENT = Decimal("0")
DEFAULT_FEE_GST_PERCENT = Decimal("18")


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _setting_decimal(key: str, default: Decimal) -> Decimal:
    try:
        obj = BookingChargeSetting.objects.filter(key=key).first()
        if obj and str(obj.value).strip() != "":
            return Decimal(str(obj.value).strip())
    except Exception:
        pass
    return default


def get_fee_percents() -> tuple[Decimal, Decimal]:
    return (
        _setting_decimal(FEE_PERCENT_KEY, DEFAULT_FEE_PERCENT),
        _setting_decimal(FEE_GST_PERCENT_KEY, DEFAULT_FEE_GST_PERCENT),
    )


def set_fee_percents(fee_percent: Decimal, fee_gst_percent: Decimal) -> None:
    fee_percent = max(Decimal("0"), Decimal(str(fee_percent)))
    fee_gst_percent = max(Decimal("0"), Decimal(str(fee_gst_percent)))
    BookingChargeSetting.objects.update_or_create(
        key=FEE_PERCENT_KEY, defaults={"value": str(fee_percent)}
    )
    BookingChargeSetting.objects.update_or_create(
        key=FEE_GST_PERCENT_KEY, defaults={"value": str(fee_gst_percent)}
    )


class FeeBreakup(NamedTuple):
    base_amount: Decimal
    convenience_fee: Decimal
    fee_gst: Decimal
    total_amount: Decimal
    fee_percent: Decimal
    fee_gst_percent: Decimal

    def as_dict(self) -> dict:
        return {
            "base_amount": str(self.base_amount),
            "convenience_fee": str(self.convenience_fee),
            "fee_gst": str(self.fee_gst),
            "total_amount": str(self.total_amount),
            "fee_percent": str(self.fee_percent),
            "fee_gst_percent": str(self.fee_gst_percent),
        }


def compute_fee_breakup(base_amount: Decimal) -> FeeBreakup:
    base = _quantize_money(max(Decimal("0"), Decimal(str(base_amount))))
    fee_percent, fee_gst_percent = get_fee_percents()
    fee = _quantize_money(base * fee_percent / Decimal("100"))
    fee_gst = _quantize_money(fee * fee_gst_percent / Decimal("100"))
    total = _quantize_money(base + fee + fee_gst)
    return FeeBreakup(
        base_amount=base,
        convenience_fee=fee,
        fee_gst=fee_gst,
        total_amount=total,
        fee_percent=fee_percent,
        fee_gst_percent=fee_gst_percent,
    )
