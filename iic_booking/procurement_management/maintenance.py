"""Equipment maintenance history: downtime, cause, action, cost and parts used from stock.

A record can be created on its own (equipment page), from the "back to functional" prompt of a disruption, or
alongside a follow-up requirement. Parts used are stock issues (reason "used in repair") linked to the record and
the equipment, so the stock ledger, the equipment's history and the cost roll-up all agree.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime

from . import access, audit
from . import constants as c
from .api import choice, parse_bool, parse_int, parse_money, parse_qty, req_str
from .errors import ProcurementError, forbidden, not_found
from .models import AMCServiceRecord, Asset, Item, MaintenanceRecord, StockTransaction, Vendor
from .numbering import next_number

P = c.OfficePermission
R = c.ModuleRole
FIELDS = ("kind", "downtime_start", "downtime_end", "cause", "action_taken", "vendor", "service_provider",
          "service_report_reference", "service_cost", "other_cost", "under_warranty_or_amc", "remarks", "asset")
MAX_PARTS = 50


def records_qs():
    return MaintenanceRecord.objects.select_related(
        "department", "equipment", "asset", "vendor", "recorded_by"
    ).filter(is_archived=False)


def visible_q(scope) -> Q:
    return access.visible_department_wide_q(scope, extra=access.lab_staff_equipment_q(scope))


def get_visible(scope, pk) -> MaintenanceRecord:
    return access.get_visible(records_qs(), scope, pk, visible_q(scope))


def can_record(scope, dept_id, equipment_id) -> bool:
    return (
        scope.is_lab_staff_for(equipment_id)
        or scope.has_role(dept_id, R.OC_STORES)
        or scope.has_role(dept_id, R.MAIN_ADMIN)
        or scope.has_perm(dept_id, P.ASSETS)
    )


def _when(raw, name):
    if raw in (None, ""):
        return None
    value = parse_datetime(str(raw))
    if value is None:
        day = parse_date(str(raw))
        if day is None:
            raise ProcurementError(f"{name} must be a date / time.", code="invalid", field=name)
        from datetime import datetime, time

        value = datetime.combine(day, time(9, 0))
    if timezone.is_naive(value):
        value = timezone.make_aware(value)
    return value


def _equipment(raw):
    from iic_booking.equipment.models import Equipment

    eq = Equipment.objects.filter(pk=parse_int(raw, "equipment_id", required=True)).first()
    if eq is None or not eq.internal_department_id:
        raise ProcurementError("Unknown equipment.", code="invalid_equipment", field="equipment_id")
    return eq


def _apply(rec: MaintenanceRecord, data: dict, *, creating: bool) -> None:
    dept_id = rec.department_id
    if creating or "kind" in data:
        rec.kind = choice(data.get("kind"), c.MaintenanceKind.values, "kind", default=c.MaintenanceKind.BREAKDOWN)
    for f in ("downtime_start", "downtime_end"):
        if f in data:
            setattr(rec, f, _when(data.get(f), f))
    if rec.downtime_start and rec.downtime_end and rec.downtime_end < rec.downtime_start:
        raise ProcurementError("Downtime cannot end before it starts.", code="invalid", field="downtime_end")
    for f, limit in (("cause", 5000), ("action_taken", 5000), ("remarks", 5000), ("service_provider", 255),
                     ("service_report_reference", 120)):
        if f in data:
            setattr(rec, f, req_str(data, f, max_len=limit, required=False))
    for f in ("service_cost", "other_cost"):
        if f in data:
            setattr(rec, f, parse_money(data.get(f) or "0", f))
    if "under_warranty_or_amc" in data:
        rec.under_warranty_or_amc = parse_bool(data.get("under_warranty_or_amc"))
    if "vendor_id" in data:
        vid = parse_int(data.get("vendor_id"), "vendor_id")
        rec.vendor = Vendor.objects.filter(pk=vid, department_id=dept_id, is_archived=False).first() if vid else None
        if vid and rec.vendor is None:
            raise not_found("Vendor not found.")
    if "asset_id" in data:
        aid = parse_int(data.get("asset_id"), "asset_id")
        rec.asset = Asset.objects.filter(pk=aid, department_id=dept_id, is_archived=False).first() if aid else None
        if aid and rec.asset is None:
            raise not_found("Asset not found.")
    if "amc_record_id" in data:
        amc_id = parse_int(data.get("amc_record_id"), "amc_record_id")
        rec.amc_record = AMCServiceRecord.objects.filter(pk=amc_id, department_id=dept_id).first() if amc_id else None
        if amc_id and rec.amc_record is None:
            raise not_found("AMC record not found.")


def _unit_cost(item: Item) -> Decimal | None:
    return (
        StockTransaction.objects.filter(item=item, tx_type=c.StockTxType.RECEIPT, unit_cost__isnull=False)
        .order_by("-transaction_date", "-id")
        .values_list("unit_cost", flat=True)
        .first()
    )


def issue_parts(scope, rec: MaintenanceRecord, parts, *, request=None) -> list[StockTransaction]:
    """Issue parts from stock against the record. OC Stores (stock permission) may draw from any store; lab staff
    only from a laboratory store."""
    from . import stock

    if parts in (None, "", []):
        return []
    if not isinstance(parts, list) or len(parts) > MAX_PARTS:
        raise ProcurementError(f"parts must be a list of at most {MAX_PARTS}.", code="invalid", field="parts")
    store_keeper = scope.has_perm(rec.department_id, P.STOCK)
    out = []
    added = Decimal("0.00")
    for idx, raw in enumerate(parts):
        if not isinstance(raw, dict):
            raise ProcurementError("Each part needs an item and quantity.", code="invalid", field=f"parts[{idx}]")
        item = Item.objects.select_related("category", "department").filter(
            pk=parse_int(raw.get("item_id"), f"parts[{idx}].item_id", required=True), department_id=rec.department_id,
            is_archived=False,
        ).first()
        if item is None:
            raise not_found("Item not found.")
        qty = parse_qty(raw.get("quantity"), f"parts[{idx}].quantity")
        lab = stock._lab(raw.get("laboratory_id"), rec.department_id)
        if not store_keeper and lab is None:
            raise forbidden("Parts from the central store are issued by OC Stores — choose your laboratory store or ask Stores.")
        tx = stock.post(
            scope, department_id=rec.department_id, item=item, tx_type=c.StockTxType.ISSUE, quantity=qty, laboratory=lab,
            reference_type="Maintenance", reference_number=rec.number, issued_to=scope.user,
            remarks=f"Used in {rec.get_kind_display().lower()} {rec.number} of {rec.equipment.name}",
            reason_code=c.StockReason.CONSUMED_IN_REPAIR, equipment=rec.equipment, maintenance_record=rec, request=request,
        )
        cost = _unit_cost(item)
        if cost is not None:
            added += (cost * qty).quantize(Decimal("0.01"))
        out.append(tx)
    if added:
        rec.parts_cost = (rec.parts_cost or Decimal("0.00")) + added
        rec.save(update_fields=["parts_cost", "updated_at"])
    return out


@transaction.atomic
def create(scope, data: dict, *, request=None, disruption_event=None) -> MaintenanceRecord:
    eq = _equipment(data.get("equipment_id")) if disruption_event is None else disruption_event.equipment
    dept_id = eq.internal_department_id
    if dept_id not in scope.enabled:
        raise forbidden("Procurement & Assets is not enabled for this equipment's department.")
    access.require_config(dept_id)
    if not can_record(scope, dept_id, eq.pk):
        raise forbidden("Only the equipment's OIC, Lab In Charge, operators or OC Stores can record maintenance.")
    if disruption_event is None and data.get("disruption_event_id"):
        from iic_booking.equipment.disruption_models import DisruptionEvent

        disruption_event = DisruptionEvent.objects.filter(pk=parse_int(data.get("disruption_event_id"), "disruption_event_id"), equipment=eq).first()
        if disruption_event is None:
            raise not_found("Disruption not found for this equipment.")
    rec = MaintenanceRecord(
        number=next_number(c.NumberPrefix.MAINTENANCE), department_id=dept_id, equipment=eq,
        disruption_event=disruption_event, recorded_by=scope.user,
    )
    if disruption_event is not None:
        rec.downtime_start = disruption_event.start_at
        rec.downtime_end = disruption_event.end_at
        rec.cause = (getattr(disruption_event, "reason", "") or "")[:5000]
        rec.action_taken = (getattr(disruption_event, "action_taken", "") or "")[:5000]
    _apply(rec, data, creating=True)
    rec.save()
    issue_parts(scope, rec, data.get("parts"), request=request)
    audit.record(scope.user, "maintenance.recorded", rec, new=audit.snapshot(rec, FIELDS), request=request)
    return rec


@transaction.atomic
def update(scope, rec: MaintenanceRecord, data: dict, *, request=None) -> MaintenanceRecord:
    rec = MaintenanceRecord.objects.select_for_update().get(pk=rec.pk)
    if not can_record(scope, rec.department_id, rec.equipment_id):
        raise forbidden()
    before = audit.snapshot(rec, FIELDS)
    _apply(rec, data, creating=False)
    rec.save()
    issue_parts(scope, rec, data.get("parts"), request=request)
    old, new = audit.diff(before, audit.snapshot(rec, FIELDS))
    if new or data.get("parts"):
        audit.record(scope.user, "maintenance.updated", rec, old=old, new={**new, "parts_added": len(data.get("parts") or [])}, request=request)
    return rec


@transaction.atomic
def raise_request(scope, rec: MaintenanceRecord, data: dict, *, request=None):
    """Create (and by default submit) a requirement pre-linked to the maintenance record and its disruption."""
    from . import requests_service, workflow

    payload = dict(data)
    payload["equipment_id"] = rec.equipment_id
    payload.setdefault("title", f"Requirement after {rec.get_kind_display().lower()} — {rec.equipment.name}"[:255])
    payload.setdefault(
        "justification",
        f"Raised from maintenance record {rec.number}." + (f" Cause: {rec.cause}" if rec.cause else ""),
    )
    pr = requests_service.create_request(scope, payload, request=request)
    pr.maintenance_record = rec
    pr.disruption_event_id = rec.disruption_event_id
    pr.save(update_fields=["maintenance_record", "disruption_event", "updated_at"])
    submitted, submit_error = False, ""
    if parse_bool(data.get("submit", True)):
        try:
            with transaction.atomic():
                pr = workflow.submit(scope, pr, request=request)
            submitted = True
        except ProcurementError as exc:
            submit_error = exc.message
    if rec.disruption_event_id:
        ev = rec.disruption_event
        ev.procurement_request_ids = [*(ev.procurement_request_ids or []), pr.pk]
        ev.save(update_fields=["procurement_request_ids", "updated_at"])
    audit.record(scope.user, "maintenance.request_raised", rec, new={"request": pr.number, "submitted": submitted}, request=request)
    return pr, submitted, submit_error


def equipment_overview(scope, equipment) -> dict:
    """Everything procurement knows about one equipment, for its profile page."""
    from . import serializers as s
    from .models import PurchaseRequest

    dept_id = equipment.internal_department_id
    if not dept_id or dept_id not in scope.enabled:
        raise not_found()
    assets = Asset.objects.filter(equipment=equipment, is_archived=False).select_related(
        "register", "category", "parent", "department", "laboratory", "equipment", "procurement_record", "vendor",
        "custodian", "created_by",
    )
    records = records_qs().filter(equipment=equipment)
    open_requests = PurchaseRequest.objects.filter(equipment=equipment).exclude(
        status__in=[c.RequestStatus.DRAFT, c.RequestStatus.CANCELLED, c.RequestStatus.REJECTED, c.RequestStatus.ISSUED]
    )
    total_downtime = sum((r.downtime_hours or 0) for r in records)
    return {
        "equipment": s.equipment_brief(equipment),
        "department_id": dept_id,
        "can_record_maintenance": can_record(scope, dept_id, equipment.pk),
        "can_raise_request": access.can_raise_for_equipment(scope, dept_id, equipment),
        "assets": [s.asset(a) for a in assets[:100]],
        "maintenance": [s.maintenance_record(r) for r in records[:50]],
        "maintenance_totals": {
            "count": records.count(),
            "downtime_hours": round(total_downtime, 1),
            "cost": s.m(sum((r.total_cost for r in records), Decimal("0.00"))),
        },
        "open_requests": [
            {"id": r.pk, "number": r.number, "title": r.title, "status": r.status, "status_label": r.get_status_display(),
             "estimated_total": s.m(r.estimated_total)}
            for r in open_requests.order_by("-created_at")[:20]
        ],
    }
