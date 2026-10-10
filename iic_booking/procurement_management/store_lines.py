"""OC Stores line editing while a request sits at the Stores stage.

Stores can change quantities and prices, substitute an equivalent item, add or drop lines and mark each line
"issue from stock" or "procure". The requester's original values are kept on the line (``store_original``) and
every edit is an ``ApprovalAction`` (STORES_EDIT) plus an audit entry, so nothing is silently rewritten.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction

from . import access, audit, notify
from . import constants as c
from .api import parse_int, parse_money, parse_qty, parse_rate
from .errors import ProcurementError
from .models import Item, PurchaseRequestLine
from .requests_service import line_total

S = c.ApprovalStage
A = c.ApprovalActionType
EDITABLE = ("item_id", "description", "specification", "quantity", "uom", "estimated_unit_price", "gst_rate")


def _snapshot(line: PurchaseRequestLine) -> dict:
    return {
        "item_id": line.item_id,
        "description": line.description,
        "quantity": str(line.quantity),
        "uom": line.uom,
        "estimated_unit_price": str(line.estimated_unit_price),
        "gst_rate": str(line.gst_rate),
    }


def _item(dept_id, raw, field):
    if raw in (None, ""):
        return None
    item = (
        Item.objects.filter(pk=parse_int(raw, field), department_id=dept_id, is_archived=False, active=True)
        .select_related("default_gst_rate")
        .first()
    )
    if item is None:
        raise ProcurementError("Unknown item.", code="invalid_item", field=field)
    return item


def _fulfilment(raw, field):
    value = str(raw or "").upper()
    if value not in c.LineFulfilment.values:
        raise ProcurementError("fulfilment must be STOCK, PROCURE or blank.", code="invalid_choice", field=field)
    return value


def _apply(line: PurchaseRequestLine, raw: dict, dept_id: int, idx: int) -> list[str]:
    changed = []
    if "item_id" in raw:
        item = _item(dept_id, raw.get("item_id"), f"lines[{idx}].item_id")
        if (item.pk if item else None) != line.item_id:
            line.item = item
            changed.append("item")
            if item and not str(raw.get("description") or "").strip():
                line.description = item.name[:255]
                line.uom = item.uom or line.uom
                if item.default_gst_rate_id and raw.get("gst_rate") in (None, ""):
                    line.gst_rate = item.default_gst_rate.rate
    if str(raw.get("description") or "").strip() and raw["description"].strip()[:255] != line.description:
        line.description = raw["description"].strip()[:255]
        changed.append("description")
    if "specification" in raw:
        spec = str(raw.get("specification") or "")[:10000]
        if spec != line.specification:
            line.specification = spec
            changed.append("specification")
    if raw.get("uom"):
        uom = str(raw["uom"])[:30]
        if uom != line.uom:
            line.uom = uom
            changed.append("uom")
    if raw.get("quantity") not in (None, ""):
        q = parse_qty(raw.get("quantity"), f"lines[{idx}].quantity")
        if q < line.issued_quantity:
            raise ProcurementError(
                "Quantity cannot be less than what has already been issued.", code="invalid_quantity", field=f"lines[{idx}].quantity"
            )
        if q != line.quantity:
            line.quantity = q
            changed.append("quantity")
    if raw.get("estimated_unit_price") not in (None, ""):
        price = parse_money(raw.get("estimated_unit_price"), f"lines[{idx}].estimated_unit_price")
        if price != line.estimated_unit_price:
            line.estimated_unit_price = price
            changed.append("estimated_unit_price")
    if raw.get("gst_rate") not in (None, ""):
        gst = parse_rate(raw.get("gst_rate"), f"lines[{idx}].gst_rate")
        if gst != line.gst_rate:
            line.gst_rate = gst
            changed.append("gst_rate")
    if "fulfilment" in raw:
        f = _fulfilment(raw.get("fulfilment"), f"lines[{idx}].fulfilment")
        if f != line.fulfilment:
            line.fulfilment = f
            changed.append("fulfilment")
    if "store_note" in raw:
        note = str(raw.get("store_note") or "")[:2000]
        if note != line.store_note:
            line.store_note = note
            changed.append("store_note")
    line.line_total = line_total(line.quantity, line.estimated_unit_price, line.gst_rate)
    return changed


@transaction.atomic
def edit_lines(scope, r, *, lines, comments: str = "", request=None):
    """Apply OC Stores edits. ``lines`` items: ``{"id": .., ...fields}`` to edit, ``{"id": .., "remove": true}``
    to drop, or a line without ``id`` to add."""
    from .workflow import _act, _ensure_approvers, _lock, _require_stage, compute_route, hod_required

    r = _lock(r)
    cfg = access.require_config(r.department)
    if r.current_stage != S.STORES or r.status not in (c.RequestStatus.PENDING_STORES,):
        raise ProcurementError("Lines can only be modified while the request is with OC Stores.", code="invalid_status")
    _require_stage(scope, r, cfg, S.STORES)
    if not isinstance(lines, list) or not lines:
        raise ProcurementError("Send the lines to change.", code="lines_required", field="lines")
    if len(lines) > 200:
        raise ProcurementError("Too many lines (max 200).", code="invalid", field="lines")

    existing = {line.pk: line for line in r.lines.select_for_update()}
    summary: list[str] = []
    for idx, raw in enumerate(lines):
        if not isinstance(raw, dict):
            raise ProcurementError("Each line must be an object.", code="invalid", field=f"lines[{idx}]")
        lid = parse_int(raw.get("id"), f"lines[{idx}].id") if raw.get("id") not in (None, "") else None
        if lid is not None:
            line = existing.get(lid)
            if line is None:
                raise ProcurementError("Unknown line.", code="invalid", field=f"lines[{idx}].id")
            if raw.get("remove"):
                if line.issued_quantity > 0:
                    raise ProcurementError("A line with stock already issued cannot be removed.", code="line_issued")
                summary.append(f"Removed “{line.description}” ({line.quantity} {line.uom})")
                line.delete()
                existing.pop(lid)
                continue
            before = _snapshot(line)
            changed = _apply(line, raw, r.department_id, idx)
            if not changed:
                continue
            if any(f in changed for f in ("item", "description", "quantity", "uom", "estimated_unit_price", "gst_rate")):
                if not line.store_original and not line.added_by_stores:
                    line.store_original = before
            line.save()
            summary.append(f"“{line.description}”: {', '.join(changed)}")
            continue
        item = _item(r.department_id, raw.get("item_id"), f"lines[{idx}].item_id")
        description = str(raw.get("description") or (item.name if item else "")).strip()
        if not description:
            raise ProcurementError("A new line needs an item or description.", code="required", field=f"lines[{idx}].description")
        qty = parse_qty(raw.get("quantity"), f"lines[{idx}].quantity")
        price = parse_money(raw.get("estimated_unit_price") or "0", f"lines[{idx}].estimated_unit_price")
        if raw.get("gst_rate") in (None, "") and item and item.default_gst_rate_id:
            gst = item.default_gst_rate.rate
        else:
            gst = parse_rate(raw.get("gst_rate") or "0", f"lines[{idx}].gst_rate")
        line = PurchaseRequestLine.objects.create(
            request=r,
            item=item,
            description=description[:255],
            specification=str(raw.get("specification") or (item.specification if item else ""))[:10000],
            quantity=qty,
            uom=str(raw.get("uom") or (item.uom if item else "Nos"))[:30],
            estimated_unit_price=price,
            gst_rate=gst,
            line_total=line_total(qty, price, gst),
            fulfilment=_fulfilment(raw.get("fulfilment"), f"lines[{idx}].fulfilment"),
            store_note=str(raw.get("store_note") or "")[:2000],
            added_by_stores=True,
        )
        existing[line.pk] = line
        summary.append(f"Added “{line.description}” ({line.quantity} {line.uom})")

    if not existing:
        raise ProcurementError("A request must keep at least one line.", code="lines_required")
    if not summary:
        return r
    old_total = r.estimated_total
    r.estimated_total = sum((line.line_total for line in existing.values()), Decimal("0.00"))
    if r.approved_amount is not None and r.estimated_total != old_total:
        r.approved_amount = None
    r.hod_required = hod_required(r, cfg)
    route = list(r.approval_route or [])
    head = route[: r.route_index + 1]
    full = compute_route(r, cfg)
    tail = full[full.index(S.STORES.value) + 1 :] if S.STORES.value in full else route[r.route_index + 1 :]
    if tail != route[r.route_index + 1 :]:
        _ensure_approvers(r, cfg, tail)
    r.approval_route = head + tail
    r.save()
    text = "; ".join(summary)
    if old_total != r.estimated_total:
        text += f". Estimated total ₹{old_total} → ₹{r.estimated_total}"
    if comments:
        text += f". {comments}"
    _act(r, scope, stage=S.STORES, action=A.STORES_EDIT, from_status=r.status, comments=text[:5000], request=request)
    notify.request_update(r, A.STORES_EDIT, scope.user, text[:500])
    return r


def suggest_fulfilment(r) -> list[dict]:
    """Per line: central-store stock on hand, so Stores can decide stock vs procure at a glance."""
    from .models import StockBalance

    item_ids = [line.item_id for line in r.lines.all() if line.item_id]
    on_hand: dict[int, Decimal] = {}
    for b in StockBalance.objects.filter(department_id=r.department_id, item_id__in=item_ids):
        on_hand[b.item_id] = on_hand.get(b.item_id, Decimal("0")) + b.quantity
    out = []
    for line in r.lines.all():
        have = on_hand.get(line.item_id) if line.item_id else None
        need = line.quantity - line.issued_quantity
        suggestion = ""
        if have is not None:
            suggestion = c.LineFulfilment.STOCK if have >= need else c.LineFulfilment.PROCURE
        out.append({"line_id": line.pk, "on_hand": str(have) if have is not None else None, "suggested": suggestion})
    return out
