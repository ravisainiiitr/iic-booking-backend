"""Asset register, status changes and controlled transfers.

* Every status change (including registration) writes an append-only ``AssetStatusHistory`` row with a reason.
* Transfer statuses are only reachable through a transfer: request (Office ``assets`` or the OIC of the asset's
  equipment) → approve / reject by a *different* user holding ``assets`` → complete (moves location, lab, equipment,
  custodian) → return (temporary transfers restore the previous placement and status).
* Disposed / retired assets are final.
* Registering assets against a procurement record satisfies its ``asset_entry_missing`` completion blocker.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import access, audit
from . import constants as c
from .api import choice, parse_day, parse_int, parse_money, req_reason, req_str
from .errors import ProcurementError, forbidden, not_found
from .models import Asset, AssetStatusHistory, AssetTransfer, ItemCategory, Item, ProcurementRecord, Vendor
from .numbering import next_number

A = c.AssetStatus
TS = c.TransferStatus
P = c.OfficePermission
MAX_BATCH = 100
LAB_REPORTABLE = frozenset({A.IN_USE, A.UNDER_REPAIR, A.DAMAGED, A.LOST})
DISPOSE_FROM = frozenset({A.CONDEMNED, A.DAMAGED, A.LOST})
OPEN_TRANSFER = frozenset({TS.REQUESTED, TS.APPROVED})
EDITABLE = ("description", "make", "model_number", "serial_number", "asset_tag", "location", "remarks")


def _history(asset, from_status, to_status, reason, actor):
    AssetStatusHistory.objects.create(
        asset=asset, from_status=from_status or "", to_status=to_status, reason=reason, changed_by=actor
    )


def _require_register(dept_id):
    cfg = access.require_config(dept_id)
    if not cfg.asset_register_enabled:
        raise ProcurementError("The asset register is not enabled for this department.", code="feature_disabled")
    return cfg


def _user(raw, name):
    uid = parse_int(raw, name)
    if not uid:
        return None
    from iic_booking.users.models import User

    user = User.objects.filter(pk=uid, is_active=True).first()
    if user is None:
        raise ProcurementError("Unknown user.", code="invalid_user", field=name)
    return user


def _equipment_in(raw, dept_id, name="equipment_id"):
    eid = parse_int(raw, name)
    if not eid:
        return None
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.filter(pk=eid, internal_department_id=dept_id).first()
    if eq is None:
        raise ProcurementError("Equipment not found in this department.", code="invalid_equipment", field=name)
    return eq


def _lab_in(raw, dept_id, name="laboratory_id"):
    if raw in (None, ""):
        return None
    from .requests_service import _lookup_laboratory

    lab = _lookup_laboratory(raw)
    if lab.department_id != dept_id:
        raise ProcurementError("Laboratory not found in this department.", code="invalid_laboratory", field=name)
    return lab


def _serials(data, count) -> list[str]:
    raw = data.get("serial_numbers")
    if raw in (None, "", []):
        single = str(data.get("serial_number") or "").strip()
        return [single] * count if count == 1 else [""] * count
    if not isinstance(raw, list) or len(raw) != count:
        raise ProcurementError("Give one serial number per asset.", code="serial_count", field="serial_numbers")
    serials = [str(x or "").strip()[:120] for x in raw]
    filled = [x for x in serials if x]
    if len(filled) != len(set(filled)):
        raise ProcurementError("Serial numbers must be unique.", code="duplicate_serial", field="serial_numbers")
    return serials


@transaction.atomic
def register(scope, data: dict, *, request=None) -> list[Asset]:
    record = None
    rec_id = parse_int(data.get("procurement_record_id"), "procurement_record_id")
    if rec_id:
        record = (
            ProcurementRecord.objects.select_for_update(of=("self",))
            .select_related("category", "selected_vendor", "purchase_request")
            .filter(pk=rec_id, department_id__in=scope.department_ids(), is_archived=False)
            .first()
        )
        if record is None:
            raise not_found("Procurement record not found.")
        if record.status == c.ProcurementRecordStatus.CANCELLED:
            raise ProcurementError("The procurement record is cancelled.", code="invalid_status")
        dept_id = record.department_id
    else:
        equipment = None
        eid = parse_int(data.get("equipment_id"), "equipment_id")
        if eid:
            from iic_booking.equipment.models import Equipment

            equipment = Equipment.objects.filter(pk=eid).first()
            if equipment is None:
                raise ProcurementError("Unknown equipment.", code="invalid_equipment", field="equipment_id")
        lab = None
        if data.get("laboratory_id") not in (None, ""):
            from .requests_service import _lookup_laboratory

            lab = _lookup_laboratory(data.get("laboratory_id"))
        if equipment is None and lab is None:
            dept_id = access.pick_department(scope, data.get("department_id")).pk
        else:
            dept_id = access.resolve_department(scope, equipment=equipment, laboratory=lab).pk
    _require_register(dept_id)
    scope.require_perm(dept_id, P.ASSETS)

    cat_id = parse_int(data.get("category_id"), "category_id") or (record.category_id if record else None)
    category = ItemCategory.objects.filter(pk=cat_id, department_id=dept_id, active=True).first() if cat_id else None
    if category is None:
        raise ProcurementError("Choose an asset category.", code="required", field="category_id")
    if not category.is_asset:
        raise ProcurementError("This category is not an asset category.", code="not_asset_category", field="category_id")
    item = None
    item_id = parse_int(data.get("item_id"), "item_id")
    if item_id:
        item = Item.objects.filter(pk=item_id, department_id=dept_id, is_archived=False).first()
        if item is None:
            raise not_found("Item not found.")
    vendor = None
    vendor_id = parse_int(data.get("vendor_id"), "vendor_id")
    if vendor_id:
        vendor = Vendor.objects.filter(pk=vendor_id, department_id=dept_id, is_archived=False).first()
        if vendor is None:
            raise not_found("Vendor not found.")
    invoice = None
    inv_id = parse_int(data.get("invoice_id"), "invoice_id")
    if inv_id:
        if record is None:
            raise ProcurementError("An invoice can only be linked through its procurement record.", code="invalid", field="invoice_id")
        invoice = record.invoices.filter(pk=inv_id, is_archived=False).first()
        if invoice is None:
            raise not_found("Invoice not found.")
    elif record is not None:
        invoice = record.invoices.filter(is_archived=False).order_by("-invoice_date", "-id").first()

    count = parse_int(data.get("count"), "count") or 1
    if not 1 <= count <= MAX_BATCH:
        raise ProcurementError(f"count must be between 1 and {MAX_BATCH}.", code="invalid", field="count")
    serials = _serials(data, count)
    clash = [s for s in serials if s and Asset.objects.filter(department_id=dept_id, serial_number__iexact=s, is_archived=False).exists()]
    if clash:
        raise ProcurementError(f"Serial number already registered: {', '.join(clash)}.", code="duplicate_serial", serials=clash)

    status = choice(data.get("status"), [A.IN_STORE, A.UNDER_INSTALLATION, A.ACTIVE, A.IN_USE], "status", default=A.IN_STORE)
    cost = parse_money(data.get("cost"), "cost", required=record is None)
    purchase_date = parse_day(data.get("purchase_date"), "purchase_date")
    if purchase_date and purchase_date > timezone.localdate():
        raise ProcurementError("The purchase date cannot be in the future.", code="future_date", field="purchase_date")
    equipment = _equipment_in(data.get("equipment_id"), dept_id) or (record.equipment if record else None)
    lab = _lab_in(data.get("laboratory_id"), dept_id) or (record.laboratory if record else None)
    common = dict(
        department_id=dept_id, laboratory=lab, equipment=equipment, item=item, category=category,
        description=req_str(data, "description", default=(record.title if record else ""), required=record is None),
        make=req_str(data, "make", max_len=120, required=False),
        model_number=req_str(data, "model_number", max_len=120, required=False),
        procurement_record=record, invoice=invoice,
        vendor=vendor or (record.selected_vendor if record else None) or (invoice.vendor if invoice else None),
        purchase_date=purchase_date or (invoice.invoice_date if invoice else None),
        cost=cost if cost is not None else _unit_cost(record, count),
        is_capitalized=bool(data.get("is_capitalized")) if "is_capitalized" in data else category.nature == c.ItemNature.MAJOR_ASSET,
        funding_type=record.funding_type if record else choice(data.get("funding_type"), c.FundingType.values, "funding_type", default=c.FundingType.OTHER),
        financial_year=record.financial_year if record else req_str(data, "financial_year", max_len=7, required=False),
        warranty_until=parse_day(data.get("warranty_until"), "warranty_until"),
        location=req_str(data, "location", required=False),
        custodian=_user(data.get("custodian_id"), "custodian_id"),
        status=status, remarks=req_str(data, "remarks", max_len=5000, required=False), created_by=scope.user,
    )
    created = []
    for i in range(count):
        asset = Asset.objects.create(
            number=next_number(c.NumberPrefix.ASSET), serial_number=serials[i],
            asset_tag=req_str(data, "asset_tag", max_len=120, required=False) if count == 1 else "", **common,
        )
        _history(asset, "", status, "Registered" + (f" from {record.number}" if record else ""), scope.user)
        audit.record(scope.user, "asset.registered", asset, new={"status": status, "cost": asset.cost, "record": getattr(record, "number", None)}, request=request)
        created.append(asset)
    if record is not None:
        _mark_asset_step(scope, record, request)
    return created


def _unit_cost(record, count) -> Decimal:
    inv_total = sum((i.total_amount for i in record.invoices.filter(is_archived=False)), Decimal("0.00"))
    base = inv_total or record.approved_amount or Decimal("0.00")
    return (base / count).quantize(Decimal("0.01"))


def _mark_asset_step(scope, record, request):
    from .purchases import CLOSED_RECORD, try_complete

    if record.status in CLOSED_RECORD:
        return
    step = c.ProcurementStep.STOCK_ASSET_ENTRY
    if step in (record.required_steps or []) and step not in (record.completed_steps or []):
        record.completed_steps = [s for s in c.PROCUREMENT_STEP_ORDER if s in set(record.completed_steps or []) | {step}]
        record.save(update_fields=["completed_steps", "updated_at"])
    try_complete(scope, record, request=request)


def _lock(asset) -> Asset:
    return Asset.objects.select_for_update(of=("self",)).get(pk=asset.pk)


@transaction.atomic
def update(scope, asset: Asset, data: dict, *, request=None) -> Asset:
    asset = _lock(asset)
    _require_register(asset.department_id)
    scope.require_perm(asset.department_id, P.ASSETS)
    if asset.status in c.ASSET_FINAL_STATUSES:
        raise ProcurementError("Disposed or retired assets cannot be edited.", code="asset_final")
    fields = list(EDITABLE) + ["custodian", "warranty_until", "is_capitalized", "cost"]
    before = audit.snapshot(asset, fields)
    for f in EDITABLE:
        if f in data:
            limit = 5000 if f == "remarks" else (255 if f in ("description", "location") else 120)
            setattr(asset, f, req_str(data, f, max_len=limit, required=f == "description"))
    if asset.serial_number and "serial_number" in data and Asset.objects.filter(
        department_id=asset.department_id, serial_number__iexact=asset.serial_number, is_archived=False
    ).exclude(pk=asset.pk).exists():
        raise ProcurementError("Serial number already registered.", code="duplicate_serial", field="serial_number")
    if "custodian_id" in data:
        asset.custodian = _user(data.get("custodian_id"), "custodian_id")
    if "warranty_until" in data:
        asset.warranty_until = parse_day(data.get("warranty_until"), "warranty_until")
    if "is_capitalized" in data:
        asset.is_capitalized = bool(data.get("is_capitalized"))
    reason = ""
    if "cost" in data:
        new_cost = parse_money(data.get("cost"), "cost")
        if new_cost != asset.cost:
            reason = req_reason(data)
            asset.cost = new_cost
    asset.save()
    old, new = audit.diff(before, audit.snapshot(asset, fields))
    if new:
        audit.record(scope.user, "asset.updated", asset, old=old, new=new, reason=reason, request=request)
    return asset


@transaction.atomic
def change_status(scope, asset: Asset, data: dict, *, request=None) -> Asset:
    asset = _lock(asset)
    _require_register(asset.department_id)
    to_status = choice(data.get("status"), A.values, "status")
    reason = req_reason(data)
    full = scope.has_perm(asset.department_id, P.ASSETS)
    if not full and not (scope.is_oic_for(asset.equipment_id) and to_status in LAB_REPORTABLE):
        raise forbidden()
    if asset.status in c.ASSET_FINAL_STATUSES:
        raise ProcurementError("Disposed or retired assets are final.", code="asset_final")
    if to_status in c.ASSET_TRANSFER_STATUSES:
        raise ProcurementError("Use an asset transfer to move an asset.", code="use_transfer")
    if to_status == asset.status:
        raise ProcurementError("The asset already has this status.", code="no_change")
    if asset.transfers.filter(status__in=OPEN_TRANSFER).exists():
        raise ProcurementError("Finish or cancel the open transfer first.", code="transfer_open")
    if asset.status == A.TEMPORARILY_TRANSFERRED:
        raise ProcurementError("Record the return of the temporary transfer first.", code="transfer_open")
    if to_status == A.DISPOSED and asset.status not in DISPOSE_FROM:
        raise ProcurementError("Only condemned, damaged or lost assets can be disposed.", code="invalid_transition")
    from_status = asset.status
    asset.status = to_status
    asset.save(update_fields=["status", "updated_at"])
    _history(asset, from_status, to_status, reason, scope.user)
    audit.record(scope.user, "asset.status_changed", asset, old={"status": from_status}, new={"status": to_status}, reason=reason, request=request)
    return asset


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------
@transaction.atomic
def request_transfer(scope, asset: Asset, data: dict, *, request=None) -> AssetTransfer:
    asset = _lock(asset)
    dept_id = asset.department_id
    _require_register(dept_id)
    if not (scope.has_perm(dept_id, P.ASSETS) or scope.is_oic_for(asset.equipment_id)):
        raise forbidden()
    if asset.status in c.ASSET_FINAL_STATUSES or asset.status in {A.LOST, A.CONDEMNED}:
        raise ProcurementError("This asset cannot be transferred.", code="invalid_status")
    if asset.status == A.TEMPORARILY_TRANSFERRED or asset.transfers.filter(status__in=OPEN_TRANSFER).exists():
        raise ProcurementError("This asset already has an open transfer.", code="transfer_open")
    ttype = choice(data.get("transfer_type"), c.TransferType.values, "transfer_type")
    to_lab = _lab_in(data.get("to_laboratory_id"), dept_id, "to_laboratory_id")
    to_eq = _equipment_in(data.get("to_equipment_id"), dept_id, "to_equipment_id")
    to_location = req_str(data, "to_location", required=False)
    to_custodian = _user(data.get("to_custodian_id"), "to_custodian_id")
    if not any([to_lab, to_eq, to_location, to_custodian]):
        raise ProcurementError("Give the destination laboratory, equipment, location or custodian.", code="destination_required")
    expected = parse_day(data.get("expected_return_date"), "expected_return_date", required=ttype == c.TransferType.TEMPORARY)
    if expected and expected < timezone.localdate():
        raise ProcurementError("The expected return date is in the past.", code="invalid_date", field="expected_return_date")
    t = AssetTransfer.objects.create(
        number=next_number(c.NumberPrefix.TRANSFER), asset=asset, department_id=dept_id, transfer_type=ttype,
        from_laboratory=asset.laboratory, to_laboratory=to_lab, from_equipment=asset.equipment, to_equipment=to_eq,
        from_location=asset.location, to_location=to_location, from_custodian=asset.custodian, to_custodian=to_custodian,
        from_status=asset.status, reason=req_reason(data), expected_return_date=expected if ttype == c.TransferType.TEMPORARY else None,
        requested_by=scope.user,
    )
    audit.record(scope.user, "asset.transfer_requested", t, new={"asset": asset.number, "type": ttype}, reason=t.reason, request=request)
    from . import notify

    notify.notify(
        notify.office_users(dept_id, P.ASSETS) + notify.department_role_users(dept_id, c.ModuleRole.OC_STORES),
        department_id=dept_id,
        title=f"Asset transfer {t.number} needs a decision", message=f"{asset.number} {asset.description}: {t.reason}",
        link=f"/procurement/assets/{asset.pk}", event="asset_transfer", actor=scope.user, extra={"transfer_id": t.pk},
    )
    return t


def _lock_transfer(t) -> AssetTransfer:
    return AssetTransfer.objects.select_for_update(of=("self",)).select_related("asset").get(pk=t.pk)


@transaction.atomic
def decide_transfer(scope, t: AssetTransfer, data: dict, *, request=None) -> AssetTransfer:
    t = _lock_transfer(t)
    _require_register(t.department_id)
    scope.require_perm(t.department_id, P.ASSETS)
    if t.requested_by_id == scope.user.pk:
        raise forbidden("You cannot decide a transfer you requested.")
    if t.status != TS.REQUESTED:
        raise ProcurementError("This transfer is not awaiting a decision.", code="invalid_status")
    decision = choice(str(data.get("decision") or "").upper(), ["APPROVE", "REJECT"], "decision")
    note = req_str(data, "note", max_len=2000, required=decision == "REJECT")
    t.status = TS.APPROVED if decision == "APPROVE" else TS.REJECTED
    t.decided_by, t.decided_at, t.decision_note = scope.user, timezone.now(), note
    t.save(update_fields=["status", "decided_by", "decided_at", "decision_note", "updated_at"])
    audit.record(scope.user, f"asset.transfer_{t.status.lower()}", t, new={"status": t.status}, reason=note, request=request)
    return t


@transaction.atomic
def complete_transfer(scope, t: AssetTransfer, *, request=None) -> AssetTransfer:
    t = _lock_transfer(t)
    _require_register(t.department_id)
    scope.require_perm(t.department_id, P.ASSETS)
    if t.status != TS.APPROVED:
        raise ProcurementError("Only approved transfers can be completed.", code="invalid_status")
    asset = _lock(t.asset)
    if t.to_laboratory_id:
        asset.laboratory_id = t.to_laboratory_id
    if t.to_equipment_id:
        asset.equipment_id = t.to_equipment_id
    if t.to_location:
        asset.location = t.to_location
    if t.to_custodian_id:
        asset.custodian_id = t.to_custodian_id
    new_status = A.TEMPORARILY_TRANSFERRED if t.transfer_type == c.TransferType.TEMPORARY else A.PERMANENTLY_TRANSFERRED
    from_status = asset.status
    asset.status = new_status
    asset.save()
    _history(asset, from_status, new_status, f"Transfer {t.number}: {t.reason}", scope.user)
    t.status, t.completed_at = TS.COMPLETED, timezone.now()
    t.save(update_fields=["status", "completed_at", "updated_at"])
    audit.record(scope.user, "asset.transfer_completed", t, new={"asset_status": new_status}, request=request)
    return t


@transaction.atomic
def return_transfer(scope, t: AssetTransfer, data: dict, *, request=None) -> AssetTransfer:
    t = _lock_transfer(t)
    _require_register(t.department_id)
    scope.require_perm(t.department_id, P.ASSETS)
    if t.transfer_type != c.TransferType.TEMPORARY or t.status != TS.COMPLETED:
        raise ProcurementError("Only completed temporary transfers can be returned.", code="invalid_status")
    asset = _lock(t.asset)
    asset.laboratory_id, asset.equipment_id = t.from_laboratory_id, t.from_equipment_id
    asset.location, asset.custodian_id = t.from_location, t.from_custodian_id
    from_status = asset.status
    asset.status = t.from_status or A.IN_USE
    asset.save()
    note = req_str(data, "note", max_len=2000, required=False)
    _history(asset, from_status, asset.status, f"Returned from transfer {t.number}" + (f": {note}" if note else ""), scope.user)
    t.status, t.returned_at = TS.RETURNED, timezone.now()
    t.save(update_fields=["status", "returned_at", "updated_at"])
    audit.record(scope.user, "asset.transfer_returned", t, new={"asset_status": asset.status}, reason=note, request=request)
    return t


@transaction.atomic
def cancel_transfer(scope, t: AssetTransfer, data: dict, *, request=None) -> AssetTransfer:
    t = _lock_transfer(t)
    if not (t.requested_by_id == scope.user.pk or scope.has_perm(t.department_id, P.ASSETS)):
        raise forbidden()
    if t.status not in OPEN_TRANSFER:
        raise ProcurementError("Only open transfers can be cancelled.", code="invalid_status")
    reason = req_reason(data)
    t.status = TS.CANCELLED
    t.decision_note = reason
    t.save(update_fields=["status", "decision_note", "updated_at"])
    audit.record(scope.user, "asset.transfer_cancelled", t, reason=reason, request=request)
    return t
