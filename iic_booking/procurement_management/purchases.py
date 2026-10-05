"""Small purchases and the shared completion check for procurement records.

A *direct* small purchase (no prior request) is only accepted when the bill total including GST is within the
department's configured threshold (or the category is approval-exempt) and the department allows Office direct
entries. A small purchase *against an approved request* is authorised by that approval; its bill is checked for
variance against the approved amount instead.
"""

from __future__ import annotations

import uuid

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import access, audit, documents, invoices
from . import constants as c
from .api import choice, parse_day, parse_int, req_str
from .errors import ProcurementError, not_found
from .fy import fy_label
from .models import ApprovalAction, ItemCategory, ProcurementDocument, ProcurementRecord, PurchaseRequest
from .numbering import next_number
from .requests_service import _lookup_equipment, _lookup_laboratory, check_features
from .small_purchase import evaluate

P = c.OfficePermission
RS = c.RequestStatus
PRS = c.ProcurementRecordStatus
CLOSED_RECORD = frozenset({PRS.COMPLETED, PRS.CANCELLED})


def completion_blockers(record: ProcurementRecord, cfg) -> list[str]:
    blockers = []
    live_invoices = record.invoices.filter(is_archived=False)
    if cfg.require_invoice:
        if not live_invoices.exists():
            blockers.append("invoice_missing")
        elif not ProcurementDocument.objects.filter(
            Q(procurement_record=record) | Q(invoice__procurement_record=record),
            doc_type=c.DocumentType.INVOICE,
            is_archived=False,
        ).exists():
            blockers.append("bill_document_missing")
    if live_invoices.filter(variance_status__in=invoices.OPEN_VARIANCE).exists():
        blockers.append("variance_open")
    if (
        cfg.require_asset_allocation
        and cfg.asset_register_enabled
        and record.category_id
        and record.category.is_asset
        and not record.assets.filter(is_archived=False).exists()
    ):
        blockers.append("asset_entry_missing")
    pending_steps = set(record.required_steps or []) - set(record.completed_steps or []) - {
        c.ProcurementStep.PAYMENT,
        c.ProcurementStep.STOCK_ASSET_ENTRY,
    }
    if pending_steps:
        blockers.append("steps_incomplete")
    return blockers


def try_complete(scope, record: ProcurementRecord, *, request=None) -> list[str]:
    """Complete the record (and its request) once nothing blocks it. Returns the remaining blockers."""
    record = ProcurementRecord.objects.select_for_update(of=("self",)).select_related("category", "purchase_request").get(pk=record.pk)
    if record.status in CLOSED_RECORD:
        return []
    cfg = access.require_config(record.department_id)
    blockers = completion_blockers(record, cfg)
    r = record.purchase_request
    if blockers:
        if r is not None and r.status in (RS.IN_PROCUREMENT, RS.AWAITING_INVOICE) and record.invoices.filter(is_archived=False).exists():
            _set_request_status(scope, r, RS.AWAITING_RECEIPT)
        return blockers
    before = record.status
    record.status = PRS.COMPLETED
    record.completed_at = timezone.now()
    record.save(update_fields=["status", "completed_at", "updated_at"])
    audit.record(scope.user, "procurement.completed", record, old={"status": before}, new={"status": record.status}, request=request)
    record.requirements.filter(status=c.RequirementStatus.PROCUREMENT_IN_PROGRESS).update(
        status=c.RequirementStatus.PROCURED, updated_at=timezone.now()
    )
    if r is not None and r.status not in (RS.COMPLETED, RS.CANCELLED):
        from_status = r.status
        r.status = RS.COMPLETED
        r.completed_at = timezone.now()
        r.save(update_fields=["status", "completed_at", "updated_at"])
        ApprovalAction.objects.create(
            department_id=r.department_id, purchase_request=r, stage=c.ApprovalStage.SYSTEM, action=c.ApprovalActionType.COMPLETE,
            from_status=from_status, to_status=r.status, actor=scope.user, comments=f"Completed via {record.number}",
        )
        from . import notify

        notify.request_update(r, c.ApprovalActionType.COMPLETE, scope.user)
    return []


def _set_request_status(scope, r: PurchaseRequest, status: str) -> None:
    if r.status == status:
        return
    r.status = status
    r.save(update_fields=["status", "updated_at"])


def attach_bills(scope, record, inv, files, *, request=None) -> list[ProcurementDocument]:
    group = uuid.uuid4() if len(files) > 1 else None
    return [
        documents.create_document(
            scope, record.department, f, doc_type=c.DocumentType.INVOICE,
            links={"procurement_record": record, "invoice": inv}, page_group=group, page_number=i + 1,
            description=f"Bill {inv.invoice_number}" + (f" — page {i + 1}" if group else ""), request=request,
        )
        for i, f in enumerate(files)
    ]


@transaction.atomic
def record_small_purchase(scope, data: dict, files=(), *, request=None) -> ProcurementRecord:
    files = list(files or [])
    if len(files) > 20:
        raise ProcurementError("Attach at most 20 pages.", code="too_many_files")
    invoice_data = data.get("invoice") if isinstance(data.get("invoice"), dict) else data
    purchase_date = parse_day(data.get("purchase_date"), "purchase_date") or parse_day(invoice_data.get("invoice_date"), "invoice_date", required=True)
    if purchase_date > timezone.localdate():
        raise ProcurementError("The purchase date cannot be in the future.", code="invalid_date", field="purchase_date")
    r = None
    if data.get("purchase_request_id") not in (None, ""):
        rid = parse_int(data.get("purchase_request_id"), "purchase_request_id")
        r = (
            PurchaseRequest.objects.select_for_update(of=("self",)).select_related("department", "category", "request_type")
            .filter(access.visible_requests_q(scope), pk=rid, is_archived=False).first()
        )
        if r is None:
            raise not_found()
        dept = r.department
        cfg = access.require_config(dept)
        scope.require_perm(dept.pk, P.RECORD_SMALL_PURCHASE)
        if r.status != RS.APPROVED or not r.is_small_purchase:
            raise ProcurementError("Only approved small-purchase requests can be recorded this way.", code="invalid_status")
        if r.procurement_records.filter(is_archived=False).exclude(status=PRS.CANCELLED).exists():
            raise ProcurementError("A purchase is already recorded for this request.", code="already_recorded")
        equipment, lab, category, funding, title = r.equipment, r.laboratory, r.category, r.funding_type, r.title
        approved = r.approved_amount
    else:
        equipment = _lookup_equipment(data.get("equipment_id"))
        lab = _lookup_laboratory(data.get("laboratory_id"))
        if equipment is not None or lab is not None:
            dept = access.resolve_department(scope, equipment=equipment, laboratory=lab)
        else:
            dept = access.pick_department(scope, data.get("department_id"))
        cfg = access.require_config(dept)
        scope.require_perm(dept.pk, P.RECORD_SMALL_PURCHASE)
        if not cfg.allow_office_direct_purchase_entry:
            raise ProcurementError("Direct purchase entry is switched off for this department.", code="direct_entry_disabled")
        category = ItemCategory.objects.filter(department=dept, pk=parse_int(data.get("category_id"), "category_id", required=True), active=True).first()
        if category is None:
            raise ProcurementError("Choose an active category.", code="invalid_category", field="category_id")
        funding = choice(data.get("funding_type"), c.FundingType.values, "funding_type", default=c.FundingType.OTHER)
        check_features(cfg, nature=category.nature, funding_type=funding)
        title = req_str(data, "title")
        approved = None
    record = ProcurementRecord.objects.create(
        number=next_number(c.NumberPrefix.SMALL_PURCHASE, on_date=purchase_date),
        department=dept,
        laboratory=lab,
        equipment=equipment,
        purchase_request=r,
        category=category,
        origin=c.RequestOrigin.REQUEST if r else c.RequestOrigin.DIRECT_PURCHASE,
        funding_type=funding,
        financial_year=fy_label(purchase_date),
        title=title,
        is_small_purchase=True,
        status=PRS.OPEN,
        required_steps=[c.ProcurementStep.INVOICE.value],
        approved_amount=approved,
        estimated_amount=r.estimated_total if r else 0,
        purchase_date=purchase_date,
        purchased_by_name=req_str(data, "purchased_by_name", required=False),
        remarks=req_str(data, "remarks", max_len=5000, required=False),
        created_by=scope.user,
    )
    inv = invoices.record_invoice(scope, record, invoice_data, approved_amount=approved, request=request)
    if r is None:
        ev = evaluate(cfg, inv.total_amount, category=category)
        if not ev.eligible:
            raise ProcurementError(
                f"₹{inv.total_amount} is above the small-purchase limit of ₹{ev.threshold}. Raise a request and follow "
                "the approval workflow.",
                code="above_small_purchase_threshold" if ev.reason == "above_threshold" else ev.reason,
                threshold=str(ev.threshold),
                total=str(inv.total_amount),
            )
    record.estimated_amount = record.estimated_amount or inv.total_amount
    record.status = PRS.INVOICED
    record.completed_steps = [c.ProcurementStep.INVOICE.value]
    record.save(update_fields=["estimated_amount", "status", "completed_steps", "updated_at"])
    attach_bills(scope, record, inv, files, request=request)
    if r is not None:
        from_status = r.status
        r.status = RS.AWAITING_RECEIPT
        r.save(update_fields=["status", "updated_at"])
        ApprovalAction.objects.create(
            department_id=dept.pk, purchase_request=r, stage=c.ApprovalStage.OFFICE, action=c.ApprovalActionType.MARK_PURCHASED,
            from_status=from_status, to_status=r.status, actor=scope.user, actor_role=c.ModuleRole.OFFICE,
            comments=f"Small purchase {record.number}", amount=inv.total_amount,
        )
    audit.record(
        scope.user, "purchase.small_recorded", record,
        new={"origin": record.origin, "total": inv.total_amount, "threshold": cfg.small_purchase_threshold,
             "request": r.number if r else None, "bills": len(files)},
        request=request,
    )
    try_complete(scope, record, request=request)
    return record
