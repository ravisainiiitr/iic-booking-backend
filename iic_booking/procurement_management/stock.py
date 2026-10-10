"""Consumable stock ledger.

``StockBalance`` holds the current quantity per (department, store, item) where store is a laboratory or, when
empty, the department's central store. It only ever changes through :func:`post`, which locks the balance row,
refuses to go below zero and writes an append-only ``StockTransaction`` in the same transaction — so the balance
always equals the sum of its ledger.

Only items whose category ``tracks_stock`` are ledgered. Free-text request lines and non-stock items are ignored by
the automatic hooks (issue against a request, receipt from a bill).
"""

from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import access, audit
from . import constants as c
from .api import choice, parse_day, parse_int, parse_money, parse_qty, req_str
from .errors import ProcurementError, not_found
from .models import Item, StockBalance, StockTransaction
from .numbering import next_number

T = c.StockTxType
P = c.OfficePermission
ZERO = Decimal("0.000")
MANUAL_TYPES = (T.OPENING, T.RECEIPT, T.ISSUE, T.ADJUSTMENT_IN, T.ADJUSTMENT_OUT, T.RETURN)
REASON_REQUIRED = frozenset({T.ISSUE, T.ADJUSTMENT_IN, T.ADJUSTMENT_OUT, T.RETURN})


def tracks(item) -> bool:
    return bool(item is not None and item.category.tracks_stock)


def _balance_for_update(department_id: int, laboratory, item) -> StockBalance:
    lookup = {"department_id": department_id, "item": item, "laboratory": laboratory}
    row = StockBalance.objects.select_for_update().filter(**lookup).first()
    if row is None:
        try:
            with transaction.atomic():
                StockBalance.objects.create(**lookup)
        except IntegrityError:
            pass
        row = StockBalance.objects.select_for_update().get(**lookup)
    return row


@transaction.atomic
def post(
    scope,
    *,
    department_id: int,
    item: Item,
    tx_type: str,
    quantity: Decimal,
    laboratory=None,
    unit_cost=None,
    tx_date=None,
    reference_type: str = "",
    reference_number: str = "",
    purchase_request=None,
    procurement_record=None,
    invoice=None,
    issued_to=None,
    remarks: str = "",
    request=None,
    batch_number: str = "",
    expiry_date=None,
    reason_code: str = "",
    equipment=None,
    maintenance_record=None,
) -> StockTransaction:
    if item.department_id != department_id:
        raise ProcurementError("The item belongs to another department.", code="department_mismatch", field="item_id")
    if not tracks(item):
        raise ProcurementError(f"{item.name} is not a stock-tracked item.", code="not_stock_item", field="item_id")
    if laboratory is not None and laboratory.department_id != department_id:
        raise ProcurementError("The laboratory belongs to another department.", code="lab_mismatch", field="laboratory_id")
    if quantity is None or quantity <= 0:
        raise ProcurementError("Quantity must be positive.", code="invalid_quantity", field="quantity")
    bal = _balance_for_update(department_id, laboratory, item)
    if tx_type == T.OPENING and StockTransaction.objects.filter(
        department_id=department_id, item=item, laboratory=laboratory
    ).exists():
        raise ProcurementError(
            "An opening balance can only be recorded before any other movement; use an adjustment.",
            code="opening_exists",
        )
    signed = quantity if tx_type in c.STOCK_INWARD else -quantity
    new_qty = bal.quantity + signed
    if new_qty < 0:
        raise ProcurementError(
            f"Not enough stock of {item.name}: {bal.quantity} {item.uom} available.",
            code="insufficient_stock",
            item_id=item.pk,
            available=str(bal.quantity),
            requested=str(quantity),
        )
    bal.quantity = new_qty
    bal.save(update_fields=["quantity", "updated_at"])
    tx = StockTransaction.objects.create(
        number=next_number(c.NumberPrefix.STOCK), department_id=department_id, laboratory=laboratory, item=item,
        tx_type=tx_type, quantity=quantity, balance_after=new_qty, unit_cost=unit_cost,
        transaction_date=tx_date or timezone.localdate(), reference_type=reference_type[:40],
        reference_number=reference_number[:80], purchase_request=purchase_request, procurement_record=procurement_record,
        invoice=invoice, issued_to=issued_to, remarks=remarks, performed_by=scope.user,
        batch_number=(batch_number or "")[:60], expiry_date=expiry_date, reason_code=reason_code or "",
        equipment=equipment, maintenance_record=maintenance_record,
    )
    audit.record(
        scope.user, f"stock.{tx_type.lower()}", tx, department=item.department,
        new={"item": item.code, "quantity": quantity, "balance_after": new_qty, "laboratory": getattr(laboratory, "pk", None),
             "reference": reference_number, "batch": batch_number or None, "reason_code": reason_code or None,
             "equipment": getattr(equipment, "pk", None)},
        reason=remarks, request=request,
    )
    if signed < 0 and laboratory is None:
        _alert_low_stock(bal, item, before=new_qty - signed)
    return tx


def _alert_low_stock(bal: StockBalance, item, *, before: Decimal) -> None:
    """Notify OC Stores the moment the central balance crosses its reorder / minimum level."""
    level = bal.reorder_level or bal.min_level or item.reorder_level or item.min_level
    if not level or not (bal.quantity <= level < before):
        return
    from . import notify

    notify.notify(
        notify.department_role_users(bal.department_id, c.ModuleRole.OC_STORES),
        department_id=bal.department_id,
        title=f"Low stock: {item.name}",
        message=f"{item.code} {item.name} is down to {bal.quantity} {item.uom} (reorder level {level}).",
        link="/procurement/stock?low=1",
        event="stock_low",
        extra={"item_id": item.pk},
    )


def issue_for_request(scope, r, lines, quantities, *, request=None) -> list[StockTransaction]:
    """Stores issue against an approved request: each stock-tracked line leaves the central store."""
    out = []
    for line in lines:
        item = line.item
        if not tracks(item):
            continue
        out.append(
            post(
                scope, department_id=r.department_id, item=item, tx_type=T.ISSUE, quantity=quantities[line.pk],
                reference_type="PurchaseRequest", reference_number=r.number, purchase_request=r,
                issued_to=r.requested_by, remarks=f"Issued against {r.number}", request=request,
            )
        )
    return out


def receive_for_invoice(scope, invoice, *, request=None) -> list[StockTransaction]:
    """A recorded bill brings its stock-tracked lines into the central store at the billed unit price."""
    out = []
    for line in invoice.lines.select_related("item__category"):
        if not tracks(line.item):
            continue
        out.append(
            post(
                scope, department_id=invoice.department_id, item=line.item, tx_type=T.RECEIPT, quantity=line.quantity,
                unit_cost=line.unit_price, tx_date=invoice.invoice_date, reference_type="Invoice",
                reference_number=invoice.invoice_number, procurement_record=invoice.procurement_record,
                invoice=invoice, remarks=f"Received on bill {invoice.invoice_number}", request=request,
            )
        )
    return out


def _lab(raw, department_id):
    if raw in (None, ""):
        return None
    from .requests_service import _lookup_laboratory

    lab = _lookup_laboratory(raw)
    if lab.department_id != department_id:
        raise not_found("Laboratory not found.")
    return lab


def _item(raw, department_id) -> Item:
    item_id = parse_int(raw, "item_id", required=True)
    item = Item.objects.select_related("category", "department").filter(
        pk=item_id, department_id=department_id, is_archived=False
    ).first()
    if item is None:
        raise not_found("Item not found.")
    return item


def manual_entry(scope, department, data: dict, *, request=None) -> StockTransaction:
    access.require_config(department.pk)
    scope.require_perm(department.pk, P.STOCK)
    tx_type = choice(data.get("tx_type"), MANUAL_TYPES, "tx_type")
    item = _item(data.get("item_id"), department.pk)
    reason_code = choice(data.get("reason_code"), [""] + list(c.StockReason.values), "reason_code", default="")
    if tx_type in (T.ADJUSTMENT_IN, T.ADJUSTMENT_OUT) and not reason_code:
        reason_code = c.StockReason.OTHER
    remarks = req_str(data, "remarks", max_len=2000, required=tx_type in REASON_REQUIRED)
    batch = req_str(data, "batch_number", max_len=60, required=False)
    expiry = parse_day(data.get("expiry_date"), "expiry_date")
    if item.tracks_batch and tx_type in (T.OPENING, T.RECEIPT) and not batch:
        raise ProcurementError(f"{item.name} is batch-tracked — give the batch / lot number.", code="required", field="batch_number")
    equipment = None
    eid = parse_int(data.get("equipment_id"), "equipment_id")
    if eid:
        from iic_booking.equipment.models import Equipment

        equipment = Equipment.objects.filter(pk=eid, internal_department_id=department.pk).first()
        if equipment is None:
            raise ProcurementError("Equipment not found in this department.", code="invalid_equipment", field="equipment_id")
    issued_to = None
    if tx_type == T.ISSUE:
        uid = parse_int(data.get("issued_to_id"), "issued_to_id")
        if uid:
            from iic_booking.users.models import User

            issued_to = User.objects.filter(pk=uid, is_active=True).first()
            if issued_to is None:
                raise ProcurementError("Unknown recipient.", code="invalid_user", field="issued_to_id")
    tx_date = parse_day(data.get("transaction_date"), "transaction_date")
    if tx_date and tx_date > timezone.localdate():
        raise ProcurementError("The date cannot be in the future.", code="future_date", field="transaction_date")
    return post(
        scope, department_id=department.pk, item=item, tx_type=tx_type, quantity=parse_qty(data.get("quantity")),
        laboratory=_lab(data.get("laboratory_id"), department.pk),
        unit_cost=parse_money(data.get("unit_cost"), "unit_cost", required=False), tx_date=tx_date,
        reference_type="Manual", reference_number=req_str(data, "reference_number", max_len=80, required=False),
        issued_to=issued_to, remarks=remarks, request=request, batch_number=batch, expiry_date=expiry,
        reason_code=reason_code, equipment=equipment,
    )


@transaction.atomic
def set_levels(scope, department, data: dict, *, request=None) -> StockBalance:
    access.require_config(department.pk)
    scope.require_perm(department.pk, P.STOCK)
    item = _item(data.get("item_id"), department.pk)
    if not tracks(item):
        raise ProcurementError(f"{item.name} is not a stock-tracked item.", code="not_stock_item", field="item_id")
    bal = _balance_for_update(department.pk, _lab(data.get("laboratory_id"), department.pk), item)
    before = {"min_level": bal.min_level, "reorder_level": bal.reorder_level}
    if data.get("min_level") not in (None, ""):
        bal.min_level = _non_negative(data["min_level"], "min_level")
    if data.get("reorder_level") not in (None, ""):
        bal.reorder_level = _non_negative(data["reorder_level"], "reorder_level")
    bal.save(update_fields=["min_level", "reorder_level", "updated_at"])
    audit.record(
        scope.user, "stock.levels_set", item, department=department, old=before,
        new={"min_level": bal.min_level, "reorder_level": bal.reorder_level}, request=request,
    )
    return bal


def _non_negative(raw, name) -> Decimal:
    try:
        value = Decimal(str(raw)).quantize(Decimal("0.001"))
    except Exception:
        raise ProcurementError(f"{name} must be a number.", code="invalid", field=name)
    if value < 0:
        raise ProcurementError(f"{name} cannot be negative.", code="invalid", field=name)
    return value
