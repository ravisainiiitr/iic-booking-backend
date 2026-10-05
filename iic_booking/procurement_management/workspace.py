"""Procurement workspace: start a record from an approved request or approved requirements, then walk the
configured steps (indent → specification → RFQ → quotations → comparative → vendor selection → PO → delivery →
inspection → invoice → payment → stock / asset entry). Bills and payments live in ``invoices``; completion in
``purchases.try_complete``."""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from . import access, audit
from . import constants as c
from .api import choice, parse_day, parse_int, parse_money, req_str
from .errors import ProcurementError, not_found
from .fy import fy_label
from .models import ApprovalAction, PlanRequirement, ProcurementRecord, PurchaseRequest, Quotation, Vendor
from .numbering import next_number

S = c.ProcurementStep
PRS = c.ProcurementRecordStatus
RS = c.RequestStatus
RQ = c.RequirementStatus
P = c.OfficePermission
MANUAL_STEPS = frozenset({S.INDENT, S.SPECIFICATION, S.RFQ, S.QUOTATIONS, S.COMPARATIVE, S.VENDOR_SELECTION,
                          S.PURCHASE_ORDER, S.DELIVERY, S.INSPECTION, S.STOCK_ASSET_ENTRY})
DEPENDS_ON = {
    S.COMPARATIVE: S.QUOTATIONS,
    S.VENDOR_SELECTION: S.QUOTATIONS,
    S.PURCHASE_ORDER: S.VENDOR_SELECTION,
    S.DELIVERY: S.PURCHASE_ORDER,
    S.INSPECTION: S.DELIVERY,
}
HUNDRED = Decimal("100")


def required_steps_for(type_steps, cfg, amount: Decimal) -> list[str]:
    steps = set(type_steps or c.PROCUREMENT_STEP_ORDER)
    if not (cfg.require_comparative_statement and amount > cfg.comparative_quotation_threshold):
        steps.discard(S.COMPARATIVE)
    return [s for s in c.PROCUREMENT_STEP_ORDER if s in steps]


def _require(scope, dept_id):
    scope.require_perm(dept_id, P.PROCUREMENT)


@transaction.atomic
def start_from_request(scope, r: PurchaseRequest, *, request=None) -> ProcurementRecord:
    r = PurchaseRequest.objects.select_for_update(of=("self",)).select_related("request_type", "category").get(pk=r.pk)
    cfg = access.require_config(r.department_id)
    _require(scope, r.department_id)
    if r.status != RS.APPROVED:
        raise ProcurementError("Only approved requests can move to procurement.", code="invalid_status")
    if r.procurement_records.filter(is_archived=False).exclude(status=PRS.CANCELLED).exists():
        raise ProcurementError("Procurement has already started for this request.", code="already_started")
    amount = r.approved_amount or r.estimated_total
    rec = ProcurementRecord.objects.create(
        number=next_number(c.NumberPrefix.PROCUREMENT), department_id=r.department_id, laboratory=r.laboratory,
        equipment=r.equipment, purchase_request=r, category=r.category, origin=c.RequestOrigin.REQUEST,
        funding_type=r.funding_type, financial_year=fy_label(), title=r.title, is_small_purchase=False,
        status=PRS.OPEN, required_steps=required_steps_for(r.request_type.procurement_steps, cfg, amount),
        approved_amount=amount, estimated_amount=r.estimated_total, specification=r.specification, created_by=scope.user,
    )
    from_status = r.status
    r.status = RS.IN_PROCUREMENT
    r.save(update_fields=["status", "updated_at"])
    ApprovalAction.objects.create(
        department_id=r.department_id, purchase_request=r, stage=c.ApprovalStage.OFFICE, action=c.ApprovalActionType.START_PROCUREMENT,
        from_status=from_status, to_status=r.status, actor=scope.user, actor_role=c.ModuleRole.OFFICE, comments=rec.number,
    )
    audit.record(scope.user, "procurement.started", rec, new={"request": r.number, "steps": rec.required_steps}, request=request)
    return rec


@transaction.atomic
def start_from_requirements(scope, data: dict, *, request=None) -> ProcurementRecord:
    ids = sorted({int(x) for x in (data.get("requirement_ids") or []) if str(x).isdigit()})
    if not ids:
        raise ProcurementError("Choose approved requirements.", code="required", field="requirement_ids")
    reqs = list(PlanRequirement.objects.select_for_update().filter(pk__in=ids, department_id__in=access_ids(scope)))
    if len(reqs) != len(ids):
        raise not_found()
    dept_id = reqs[0].department_id
    cfg = access.require_config(dept_id)
    _require(scope, dept_id)
    if len({(r.department_id, r.financial_year, r.funding_type) for r in reqs}) != 1:
        raise ProcurementError("Requirements must share department, financial year and funding type.", code="bucket_mismatch")
    for r in reqs:
        if r.status != RQ.APPROVED or r.procurement_record_id:
            raise ProcurementError(f"{r.number} is not an approved requirement awaiting procurement.", code="invalid_status")
    amount = sum((r.approved_amount or Decimal("0.00") for r in reqs), Decimal("0.00"))
    single = lambda attr: (lambda vals: vals.pop() if len(vals) == 1 else None)({getattr(r, attr) for r in reqs})  # noqa: E731
    proposal_ids = {r.proposal_id for r in reqs}
    rec = ProcurementRecord.objects.create(
        number=next_number(c.NumberPrefix.PROCUREMENT), department_id=dept_id, laboratory_id=single("laboratory_id"),
        equipment_id=single("equipment_id"), proposal_id=proposal_ids.pop() if len(proposal_ids) == 1 else None,
        category_id=single("category_id"), origin=c.RequestOrigin.PLAN_REQUIREMENT, funding_type=reqs[0].funding_type,
        financial_year=reqs[0].financial_year, title=req_str(data, "title"), status=PRS.OPEN,
        required_steps=required_steps_for(None, cfg, amount), approved_amount=amount,
        estimated_amount=sum((r.estimated_total for r in reqs), Decimal("0.00")),
        specification="\n\n".join(f"{r.number}: {r.specification}" for r in reqs if r.specification), created_by=scope.user,
    )
    for r in reqs:
        r.status = RQ.PROCUREMENT_IN_PROGRESS
        r.procurement_record = rec
        r.save(update_fields=["status", "procurement_record", "updated_at"])
    audit.record(scope.user, "procurement.started", rec, new={"requirements": [r.number for r in reqs], "approved": amount}, request=request)
    return rec


def access_ids(scope):
    return list(scope.department_ids())


def _lock_open(scope, rec) -> ProcurementRecord:
    rec = ProcurementRecord.objects.select_for_update(of=("self",)).select_related("purchase_request").get(pk=rec.pk)
    access.require_config(rec.department_id)
    _require(scope, rec.department_id)
    if rec.status in (PRS.COMPLETED, PRS.CANCELLED):
        raise ProcurementError("This record is closed.", code="closed")
    return rec


def _mark(rec, step) -> None:
    done = list(rec.completed_steps or [])
    if step not in done:
        done.append(step)
    rec.completed_steps = [s for s in c.PROCUREMENT_STEP_ORDER if s in done]


def _lowest_compliant(rec):
    return (
        rec.quotations.filter(is_archived=False, compliance=c.Compliance.COMPLIANT).order_by("total_amount", "id").first()
    )


@transaction.atomic
def update_step(scope, rec: ProcurementRecord, step: str, data: dict, *, request=None) -> ProcurementRecord:
    rec = _lock_open(scope, rec)
    cfg = access.get_config(rec.department_id)
    step = choice(step, list(MANUAL_STEPS), "step")
    if step not in (rec.required_steps or []):
        raise ProcurementError("This step is not required for this record.", code="not_applicable")
    dep = DEPENDS_ON.get(step)
    if dep and dep in rec.required_steps and dep not in (rec.completed_steps or []):
        raise ProcurementError(f"Complete '{S(dep).label}' first.", code="step_order", depends_on=dep)
    fields = ("indent_number", "indent_date", "specification", "rfq_reference", "rfq_date", "selected_vendor",
              "selection_justification", "po_number", "po_date", "po_amount", "expected_delivery_date", "delivery_date",
              "delivery_challan_number", "inspection_date", "inspection_result", "inspection_remarks", "status")
    before = audit.snapshot(rec, fields)
    today = timezone.localdate()

    def past_date(name):
        d = parse_day(data.get(name), name, required=True)
        if d > today:
            raise ProcurementError(f"{name} cannot be in the future.", code="invalid_date", field=name)
        return d

    if step == S.INDENT:
        rec.indent_number = req_str(data, "indent_number", max_len=80)
        rec.indent_date = past_date("indent_date")
    elif step == S.SPECIFICATION:
        rec.specification = req_str(data, "specification", max_len=20000)
    elif step == S.RFQ:
        rec.rfq_reference = req_str(data, "rfq_reference", max_len=120)
        rec.rfq_date = past_date("rfq_date")
    elif step == S.QUOTATIONS:
        if not rec.quotations.filter(is_archived=False).exists():
            raise ProcurementError("Record at least one quotation.", code="quotations_required")
    elif step == S.COMPARATIVE:
        if rec.quotations.filter(is_archived=False).count() < 2:
            raise ProcurementError("A comparative statement needs at least two quotations.", code="quotations_required")
    elif step == S.VENDOR_SELECTION:
        qid = parse_int(data.get("quotation_id"), "quotation_id", required=True)
        qt = rec.quotations.filter(pk=qid, is_archived=False).select_related("vendor").first()
        if qt is None:
            raise ProcurementError("Unknown quotation.", code="invalid", field="quotation_id")
        lowest = _lowest_compliant(rec)
        justification = req_str(data, "selection_justification", max_len=5000, required=False)
        if (lowest is None or lowest.pk != qt.pk) and not justification:
            raise ProcurementError(
                "Explain why the lowest compliant quotation was not selected.", code="justification_required",
                field="selection_justification",
            )
        rec.quotations.filter(is_selected=True).update(is_selected=False, updated_at=timezone.now())
        qt.is_selected = True
        qt.save(update_fields=["is_selected", "updated_at"])
        rec.selected_vendor = qt.vendor
        rec.selection_justification = justification
    elif step == S.PURCHASE_ORDER:
        if rec.selected_vendor_id is None:
            raise ProcurementError("Select a vendor first.", code="vendor_required")
        rec.po_number = req_str(data, "po_number", max_len=80)
        rec.po_date = past_date("po_date")
        rec.po_amount = parse_money(data.get("po_amount"), "po_amount", allow_zero=False)
        rec.expected_delivery_date = parse_day(data.get("expected_delivery_date"), "expected_delivery_date")
        if rec.approved_amount is not None:
            limit = rec.approved_amount * (HUNDRED + cfg.variance_tolerance_percent) / HUNDRED
            if rec.po_amount > limit:
                raise ProcurementError(
                    f"The PO value exceeds the approved ₹{rec.approved_amount} beyond the {cfg.variance_tolerance_percent}% "
                    "tolerance. Seek fresh approval.", code="po_exceeds_approval", field="po_amount",
                )
        rec.status = PRS.PO_ISSUED
    elif step == S.DELIVERY:
        rec.delivery_date = past_date("delivery_date")
        if rec.po_date and rec.delivery_date < rec.po_date:
            raise ProcurementError("Delivery cannot be before the PO date.", code="invalid_date", field="delivery_date")
        rec.delivery_challan_number = req_str(data, "delivery_challan_number", max_len=80, required=False)
        rec.status = PRS.DELIVERED
        r = rec.purchase_request
        if r is not None and r.status == RS.IN_PROCUREMENT:
            r.status = RS.AWAITING_INVOICE
            r.save(update_fields=["status", "updated_at"])
    elif step == S.INSPECTION:
        rec.inspection_date = past_date("inspection_date")
        rec.inspection_result = choice(
            data.get("inspection_result"),
            [c.InspectionResult.ACCEPTED, c.InspectionResult.PARTIALLY_ACCEPTED, c.InspectionResult.REJECTED],
            "inspection_result",
        )
        rec.inspection_remarks = req_str(data, "inspection_remarks", max_len=5000, required=rec.inspection_result != c.InspectionResult.ACCEPTED)
        rec.inspected_by = scope.user
    elif step == S.STOCK_ASSET_ENTRY:
        pass
    _mark(rec, step)
    if rec.status == PRS.OPEN:
        rec.status = PRS.IN_PROGRESS
    rec.save()
    old, new = audit.diff(before, audit.snapshot(rec, fields))
    audit.record(scope.user, f"procurement.step.{step.lower()}", rec, old=old, new=new, request=request)
    from .purchases import try_complete

    try_complete(scope, rec, request=request)
    return rec


@transaction.atomic
def add_quotation(scope, rec: ProcurementRecord, data: dict, *, request=None) -> Quotation:
    rec = _lock_open(scope, rec)
    vendor = Vendor.objects.filter(
        pk=parse_int(data.get("vendor_id"), "vendor_id", required=True), department_id=rec.department_id, is_archived=False
    ).first()
    if vendor is None:
        raise ProcurementError("Unknown vendor.", code="invalid_vendor", field="vendor_id")
    if rec.quotations.filter(vendor=vendor, is_archived=False).exists():
        raise ProcurementError("This vendor's quotation is already recorded.", code="duplicate", field="vendor_id")
    amount = parse_money(data.get("amount"), "amount", allow_zero=False)
    gst = parse_money(data.get("gst_amount"), "gst_amount", required=False) or Decimal("0.00")
    qt = Quotation.objects.create(
        procurement_record=rec, vendor=vendor, quotation_reference=req_str(data, "quotation_reference", max_len=120, required=False),
        quotation_date=parse_day(data.get("quotation_date"), "quotation_date"), amount=amount, gst_amount=gst,
        total_amount=amount + gst, delivery_period=req_str(data, "delivery_period", max_len=120, required=False),
        warranty=req_str(data, "warranty", max_len=120, required=False),
        compliance=choice(data.get("compliance"), c.Compliance.values, "compliance", default=c.Compliance.COMPLIANT),
        remarks=req_str(data, "remarks", max_len=5000, required=False), created_by=scope.user,
    )
    if rec.status == PRS.OPEN:
        rec.status = PRS.IN_PROGRESS
        rec.save(update_fields=["status", "updated_at"])
    audit.record(scope.user, "procurement.quotation_added", rec, new={"vendor": vendor.name, "total": qt.total_amount}, request=request)
    return qt


def comparative(rec: ProcurementRecord) -> list[dict]:
    rows = list(rec.quotations.filter(is_archived=False).select_related("vendor").order_by("total_amount", "id"))
    lowest = _lowest_compliant(rec)
    return [
        {"rank": i + 1, "quotation_id": q.pk, "vendor": q.vendor.name, "amount": str(q.amount), "gst_amount": str(q.gst_amount),
         "total_amount": str(q.total_amount), "compliance": q.compliance, "delivery_period": q.delivery_period,
         "warranty": q.warranty, "is_lowest_compliant": bool(lowest and lowest.pk == q.pk), "is_selected": q.is_selected}
        for i, q in enumerate(rows)
    ]


@transaction.atomic
def cancel_record(scope, rec: ProcurementRecord, reason: str, *, request=None) -> ProcurementRecord:
    rec = _lock_open(scope, rec)
    if rec.invoices.filter(is_archived=False, paid_amount__gt=0).exists():
        raise ProcurementError("Payments are recorded against this record.", code="has_payments")
    before = rec.status
    rec.status = PRS.CANCELLED
    rec.remarks = (rec.remarks + f"\nCancelled: {reason}").strip()
    rec.save(update_fields=["status", "remarks", "updated_at"])
    r = rec.purchase_request
    if r is not None and r.status in (RS.IN_PROCUREMENT, RS.AWAITING_INVOICE, RS.AWAITING_RECEIPT):
        from_status = r.status
        r.status = RS.APPROVED
        r.save(update_fields=["status", "updated_at"])
        ApprovalAction.objects.create(
            department_id=r.department_id, purchase_request=r, stage=c.ApprovalStage.OFFICE, action=c.ApprovalActionType.CANCEL,
            from_status=from_status, to_status=r.status, actor=scope.user, actor_role=c.ModuleRole.OFFICE,
            comments=f"Procurement {rec.number} cancelled: {reason}",
        )
    rec.requirements.filter(status=RQ.PROCUREMENT_IN_PROGRESS).update(status=RQ.APPROVED, procurement_record=None, updated_at=timezone.now())
    audit.record(scope.user, "procurement.cancelled", rec, old={"status": before}, new={"status": rec.status}, reason=reason, request=request)
    return rec
