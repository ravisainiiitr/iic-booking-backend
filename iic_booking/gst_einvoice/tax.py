"""GST calculation for an invoice. The rate always comes from a TaxRule (configured, illustrative until SRIC approves
it), never from a constant here.

* Same state (supplier state code == place of supply): CGST = SGST = taxable x rate / 2, each rounded to paise.
* Different state: IGST = taxable x rate.
* Line total = taxable + taxes; invoice total = sum of lines, rounded to the rupee with the difference in round_off
  (within the IRP's ±99.99 limit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

PAISE = Decimal("0.01")
RUPEE = Decimal("1")


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(PAISE, rounding=ROUND_HALF_UP)


@dataclass
class LineInput:
    description: str
    sac_code: str
    gst_rate: Decimal
    unit_price: Decimal
    quantity: Decimal = Decimal("1")
    unit: str = "OTH"
    detail: str = ""
    tax_rule_id: int | None = None


@dataclass
class LineResult:
    sl_no: int
    description: str
    detail: str
    sac_code: str
    quantity: Decimal
    unit: str
    unit_price: Decimal
    taxable_value: Decimal
    gst_rate: Decimal
    cgst: Decimal
    sgst: Decimal
    igst: Decimal
    total: Decimal
    tax_rule_id: int | None = None


@dataclass
class TaxResult:
    inter_state: bool
    lines: list[LineResult] = field(default_factory=list)
    taxable_value: Decimal = Decimal("0.00")
    cgst: Decimal = Decimal("0.00")
    sgst: Decimal = Decimal("0.00")
    igst: Decimal = Decimal("0.00")
    round_off: Decimal = Decimal("0.00")
    total: Decimal = Decimal("0.00")


def calculate(lines: list[LineInput], *, supplier_state: str, place_of_supply: str) -> TaxResult:
    if not lines:
        raise ValueError("An invoice needs at least one line.")
    inter = str(supplier_state).zfill(2) != str(place_of_supply).zfill(2)
    result = TaxResult(inter_state=inter)
    for i, line in enumerate(lines, start=1):
        rate = Decimal(str(line.gst_rate))
        if rate < 0 or rate > 28:
            raise ValueError(f"GST rate {rate}% is outside 0-28%.")
        qty = Decimal(str(line.quantity))
        if qty <= 0:
            raise ValueError("Quantity must be positive.")
        unit_price = money(line.unit_price)
        taxable = money(qty * unit_price)
        if inter:
            igst, cgst, sgst = money(taxable * rate / 100), Decimal("0.00"), Decimal("0.00")
        else:
            half = money(taxable * rate / 200)
            igst, cgst, sgst = Decimal("0.00"), half, half
        total = taxable + cgst + sgst + igst
        result.lines.append(LineResult(i, line.description, line.detail, line.sac_code, qty, line.unit, unit_price,
                                       taxable, rate, cgst, sgst, igst, total, line.tax_rule_id))
        result.taxable_value += taxable
        result.cgst += cgst
        result.sgst += sgst
        result.igst += igst
    exact = result.taxable_value + result.cgst + result.sgst + result.igst
    result.total = exact.quantize(RUPEE, rounding=ROUND_HALF_UP).quantize(PAISE)
    result.round_off = result.total - exact
    return result


# --- amount in words (Indian numbering) ------------------------------------------------------------------------------

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve",
         "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_thousand(n: int) -> str:
    parts = []
    if n >= 100:
        parts.append(f"{_ONES[n // 100]} Hundred")
        n %= 100
    if n >= 20:
        parts.append(_TENS[n // 10] + (f" {_ONES[n % 10]}" if n % 10 else ""))
    elif n:
        parts.append(_ONES[n])
    return " ".join(parts)


def _integer_words(n: int) -> str:
    if n == 0:
        return "Zero"
    parts = []
    for size, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        if n >= size:
            count = n // size
            parts.append(f"{_integer_words(count) if size == 10_000_000 else _below_thousand(count)} {name}")
            n %= size
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def in_words(amount) -> str:
    """'INR Ten Thousand Six Hundred Twenty Only' (paise added when present), as on SRIC's invoices."""
    value = money(amount)
    rupees = int(value)
    paise = int((value - rupees) * 100)
    text = f"INR {_integer_words(rupees)}"
    if paise:
        text += f" and {_below_thousand(paise)} Paise"
    return text + " Only"
