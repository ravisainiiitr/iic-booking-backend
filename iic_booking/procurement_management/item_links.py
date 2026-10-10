"""Equipment ↔ inventory linkage: which consumables, spares and accessories an equipment uses.

``suggested_lines`` feeds the "fill from equipment inventory" step of a requirement: every linked item (plus items
mapped through the legacy equipment inventory) with stock on hand in the central store / labs, reorder flags and the
last receipt price, so the requester picks quantities instead of typing descriptions.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models import Q

from . import access, audit
from . import constants as c
from .api import choice, parse_bool, parse_int, parse_qty, req_str
from .errors import ProcurementError, forbidden, not_found
from .models import Item, ItemEquipmentLink, StockBalance, StockTransaction

P = c.OfficePermission
LINK_FIELDS = ("usage", "typical_quantity", "notes", "active")


def links_qs():
    return ItemEquipmentLink.objects.select_related("item", "item__category", "item__default_gst_rate", "equipment")


def can_manage(scope, dept_id, equipment_id) -> bool:
    return (
        scope.has_role(dept_id, c.ModuleRole.OC_STORES)
        or scope.has_perm(dept_id, P.MASTERS)
        or scope.is_oic_for(equipment_id)
        or scope.is_incharge_for(equipment_id)
    )


def _equipment(scope, raw):
    from iic_booking.equipment.models import Equipment

    eid = parse_int(raw, "equipment_id", required=True)
    eq = Equipment.objects.filter(pk=eid).first()
    if eq is None or not eq.internal_department_id:
        raise ProcurementError("Unknown equipment.", code="invalid_equipment", field="equipment_id")
    return eq


@transaction.atomic
def save_link(scope, data: dict, link: ItemEquipmentLink | None = None, *, request=None) -> ItemEquipmentLink:
    creating = link is None
    if creating:
        eq = _equipment(scope, data.get("equipment_id"))
        dept_id = eq.internal_department_id
        access.require_config(dept_id)
        if not can_manage(scope, dept_id, eq.pk):
            raise forbidden("Only OC Stores, the Office (masters) or the equipment's OIC / Lab In Charge can link items.")
        item = Item.objects.filter(
            pk=parse_int(data.get("item_id"), "item_id", required=True), department_id=dept_id, is_archived=False
        ).first()
        if item is None:
            raise ProcurementError("Item not found in this department.", code="invalid_item", field="item_id")
        link = ItemEquipmentLink(department_id=dept_id, item=item, equipment=eq, created_by=scope.user)
        before = {}
    else:
        if not can_manage(scope, link.department_id, link.equipment_id):
            raise forbidden()
        before = audit.snapshot(link, LINK_FIELDS)
    if creating or "usage" in data:
        link.usage = choice(data.get("usage"), c.LinkUsage.values, "usage", default=c.LinkUsage.CONSUMABLE)
    if creating or "typical_quantity" in data:
        link.typical_quantity = parse_qty(data.get("typical_quantity") or "1", "typical_quantity")
    if "notes" in data:
        link.notes = req_str(data, "notes", required=False)
    if "active" in data:
        link.active = parse_bool(data.get("active"))
    try:
        with transaction.atomic():
            link.save()
    except IntegrityError:
        raise ProcurementError("This item is already linked to the equipment.", code="duplicate_link")
    old, new = audit.diff(before, audit.snapshot(link, LINK_FIELDS))
    audit.record(
        scope.user, "item_link.created" if creating else "item_link.updated", link,
        old=old, new={**new, "item": link.item.code, "equipment": link.equipment_id}, request=request,
    )
    return link


@transaction.atomic
def delete_link(scope, link: ItemEquipmentLink, *, request=None) -> None:
    if not can_manage(scope, link.department_id, link.equipment_id):
        raise forbidden()
    audit.record(scope.user, "item_link.deleted", link, old={"item": link.item.code, "equipment": link.equipment_id}, request=request)
    link.delete()


def get_link(scope, pk) -> ItemEquipmentLink:
    link = links_qs().filter(pk=pk, department_id__in=scope.department_ids()).first()
    if link is None:
        raise not_found("Link not found.")
    return link


def _last_prices(dept_id, item_ids) -> dict[int, Decimal]:
    out: dict[int, Decimal] = {}
    rows = (
        StockTransaction.objects.filter(department_id=dept_id, item_id__in=item_ids, tx_type=c.StockTxType.RECEIPT, unit_cost__isnull=False)
        .order_by("item_id", "-transaction_date", "-id")
        .values_list("item_id", "unit_cost")
    )
    for item_id, cost in rows:
        out.setdefault(item_id, cost)
    return out


def suggested_lines(scope, equipment) -> dict:
    """Linked items (and legacy-mapped items) with availability for the equipment's department."""
    dept_id = equipment.internal_department_id
    if not dept_id or dept_id not in scope.department_ids():
        raise not_found()
    links = list(links_qs().filter(equipment=equipment, active=True, item__is_archived=False, item__active=True))
    by_item = {link.item_id: link for link in links}
    legacy_ids = list(equipment.inventory_items.filter(is_enabled=True).values_list("item_id", flat=True)) if hasattr(equipment, "inventory_items") else []
    legacy_items = []
    if legacy_ids:
        legacy_items = list(
            Item.objects.filter(department_id=dept_id, legacy_inventory_item_id__in=legacy_ids, is_archived=False, active=True)
            .exclude(pk__in=list(by_item)).select_related("category", "default_gst_rate")
        )
    items = [link.item for link in links] + legacy_items
    item_ids = [i.pk for i in items]
    central: dict[int, StockBalance] = {}
    labs: dict[int, Decimal] = {}
    for b in StockBalance.objects.filter(department_id=dept_id, item_id__in=item_ids).select_related("laboratory"):
        if b.laboratory_id is None:
            central[b.item_id] = b
        else:
            labs[b.item_id] = labs.get(b.item_id, Decimal("0")) + b.quantity
    prices = _last_prices(dept_id, item_ids)
    out = []
    for it in items:
        link = by_item.get(it.pk)
        bal = central.get(it.pk)
        on_hand = bal.quantity if bal else Decimal("0")
        reorder = (bal.reorder_level if bal and bal.reorder_level else it.reorder_level) or Decimal("0")
        minimum = (bal.min_level if bal and bal.min_level else it.min_level) or Decimal("0")
        typical = link.typical_quantity if link else Decimal("1")
        price = prices.get(it.pk)
        out.append({
            "item_id": it.pk,
            "code": it.code,
            "name": it.name,
            "uom": it.uom,
            "part_number": it.part_number,
            "category": {"id": it.category_id, "name": it.category.name, "nature": it.category.nature},
            "usage": link.usage if link else c.LinkUsage.CONSUMABLE,
            "source": "link" if link else "legacy",
            "link_id": link.pk if link else None,
            "typical_quantity": str(typical),
            "notes": link.notes if link else "",
            "central_stock": str(on_hand),
            "lab_stock": str(labs.get(it.pk, Decimal("0"))),
            "reorder_level": str(reorder),
            "min_level": str(minimum),
            "reorder_due": bool(reorder and on_hand <= reorder),
            "below_min": bool(minimum and on_hand < minimum),
            "available_for_typical": on_hand >= typical,
            "last_unit_price": str(price) if price is not None else None,
            "gst_rate": str(it.default_gst_rate.rate) if it.default_gst_rate_id else None,
            "suggested_quantity": str(typical),
        })
    out.sort(key=lambda r: (not r["reorder_due"], r["usage"], r["name"]))
    return {"equipment": {"id": equipment.pk, "name": equipment.name, "code": equipment.code}, "department_id": dept_id, "results": out}


def low_stock_q() -> Q:
    from django.db.models import F

    return Q(min_level__gt=0, quantity__lt=F("min_level")) | Q(reorder_level__gt=0, quantity__lte=F("reorder_level"))
