"""Plan / non-plan requirements, Office consolidation and proposals.

Lifecycle: lab staff raise requirements (DRAFT → SUBMITTED, original values frozen). Office (``consolidate``)
edits, adds, removes, restores, merges and splits them — every change needs a reason and writes field-level
``RequirementChangeLog`` rows. Office bundles requirements of one department / FY / funding type into a proposal,
sends it to the HOD, and the HOD (in-app) or Office (offline, signed document) records the decision.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import access, audit, notify
from . import constants as c
from .api import choice, parse_day, parse_int, parse_money, parse_qty, req_str
from .errors import ProcurementError, forbidden, not_found
from .fy import fy_label, is_valid_fy_label
from .models import ApprovalAction, ItemCategory, PlanProposal, PlanRequirement, RequirementChangeLog
from .numbering import next_number
from .requests_service import _lookup_equipment, _lookup_laboratory, check_features

RQ = c.RequirementStatus
PS = c.ProposalStatus
CT = c.RequirementChangeType
P = c.OfficePermission
R = c.ModuleRole
CONSOLIDATABLE = frozenset({RQ.SUBMITTED, RQ.UNDER_CONSOLIDATION, RQ.CONSOLIDATED})
EDITABLE_FIELDS = ("description", "specification", "justification", "quantity", "uom", "estimated_unit_cost", "priority", "category")
PLAN_FUNDING = (c.FundingType.PLAN, c.FundingType.NON_PLAN)


def _total(qty: Decimal, cost: Decimal) -> Decimal:
    return (qty * cost).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def requirements_q(scope) -> Q:
    extra = access.lab_staff_equipment_q(scope)
    mine = Q(raised_by=scope.user)
    return access.visible_department_wide_q(scope, extra=(extra | mine) if extra is not None else mine)


def _log(req, change_type, user, reason, *, field="", old="", new="", related=None):
    RequirementChangeLog.objects.create(
        requirement=req, change_type=change_type, field=field, old_value="" if old is None else str(old),
        new_value="" if new is None else str(new), related_requirement=related, reason=reason, changed_by=user,
    )


def _require_consolidate(scope, dept_id):
    scope.require_perm(dept_id, P.CONSOLIDATE)


def _apply_fields(req, data, dept):
    if "description" in data:
        req.description = req_str(data, "description")
    for f, max_len in (("specification", 20000), ("justification", 10000)):
        if f in data:
            setattr(req, f, req_str(data, f, max_len=max_len, required=False))
    if "quantity" in data:
        req.quantity = parse_qty(data.get("quantity"))
    if "uom" in data:
        req.uom = req_str(data, "uom", max_len=30)
    if "estimated_unit_cost" in data:
        req.estimated_unit_cost = parse_money(data.get("estimated_unit_cost"), "estimated_unit_cost")
    if "priority" in data:
        req.priority = choice(data.get("priority"), c.Priority.values, "priority")
    if "category_id" in data:
        req.category = None
        if data.get("category_id") not in (None, ""):
            req.category = ItemCategory.objects.filter(department=dept, pk=parse_int(data["category_id"], "category_id"), active=True).first()
            if req.category is None:
                raise ProcurementError("Choose an active category.", code="invalid_category", field="category_id")
    req.estimated_total = _total(req.quantity, req.estimated_unit_cost)


def _snapshot(req) -> dict:
    return audit.snapshot(req, (*EDITABLE_FIELDS, "estimated_total"))


@transaction.atomic
def create_requirement(scope, data: dict, *, request=None) -> PlanRequirement:
    equipment = _lookup_equipment(data.get("equipment_id"))
    lab = _lookup_laboratory(data.get("laboratory_id"))
    if equipment is not None or lab is not None:
        dept = access.resolve_department(scope, equipment=equipment, laboratory=lab)
    else:
        dept = access.pick_department(scope, data.get("department_id"))
    cfg = access.require_config(dept)
    office = scope.has_perm(dept.pk, P.CONSOLIDATE)
    lab_staff = equipment is not None and (scope.is_oic_for(equipment.pk) or scope.is_operator_for(equipment.pk))
    if not (office or lab_staff):
        raise forbidden("Requirements are raised by lab staff for their equipment, or added by Office.")
    funding = choice(data.get("funding_type"), PLAN_FUNDING, "funding_type")
    check_features(cfg, funding_type=funding)
    if funding == c.FundingType.PLAN and not cfg.plan_submission_open and not office:
        raise ProcurementError("Plan submissions are closed for this cycle.", code="plan_closed")
    fy = str(data.get("financial_year") or cfg.current_financial_year or fy_label())
    if not is_valid_fy_label(fy):
        raise ProcurementError("financial_year must look like 2026-27.", code="invalid_fy", field="financial_year")
    if "quantity" not in data or "estimated_unit_cost" not in data:
        raise ProcurementError("quantity and estimated_unit_cost are required.", code="required")
    req = PlanRequirement(
        department=dept, laboratory=lab, equipment=equipment, financial_year=fy, funding_type=funding,
        raised_by=scope.user, quantity=Decimal("1.000"), estimated_unit_cost=Decimal("0.00"),
    )
    _apply_fields(req, {**data, "description": data.get("description")}, dept)
    prefix = c.NumberPrefix.PLAN if funding == c.FundingType.PLAN else c.NumberPrefix.NON_PLAN
    req.number = next_number(prefix, financial_year=fy)
    if office and not lab_staff:
        reason = req_str(data, "reason", max_len=5000)
        req.added_by_office = True
        req.status = RQ.SUBMITTED
        req.submitted_at = timezone.now()
        req.save()
        _log(req, CT.ADD, scope.user, reason, new=req.description)
    else:
        req.save()
    audit.record(scope.user, "requirement.created", req, new=_snapshot(req), request=request)
    return req


@transaction.atomic
def update_draft(scope, req: PlanRequirement, data: dict, *, request=None) -> PlanRequirement:
    req = PlanRequirement.objects.select_for_update().get(pk=req.pk)
    if req.raised_by_id != scope.user.pk or req.status != RQ.DRAFT:
        raise ProcurementError("Only your own draft requirements can be edited here.", code="not_editable")
    before = _snapshot(req)
    _apply_fields(req, data, req.department)
    req.save()
    old, new = audit.diff(before, _snapshot(req))
    audit.record(scope.user, "requirement.updated", req, old=old, new=new, request=request)
    return req


@transaction.atomic
def submit_requirement(scope, req: PlanRequirement, *, request=None) -> PlanRequirement:
    req = PlanRequirement.objects.select_for_update().get(pk=req.pk)
    cfg = access.require_config(req.department_id)
    if req.raised_by_id != scope.user.pk:
        raise forbidden()
    if req.status != RQ.DRAFT:
        raise ProcurementError("Only drafts can be submitted.", code="invalid_status")
    if req.funding_type == c.FundingType.PLAN and not cfg.plan_submission_open:
        raise ProcurementError("Plan submissions are closed for this cycle.", code="plan_closed")
    if not req.justification.strip():
        raise ProcurementError("A justification is required.", code="required", field="justification")
    req.status = RQ.SUBMITTED
    req.submitted_at = timezone.now()
    req.original_values = _snapshot(req)
    req.save()
    ApprovalAction.objects.create(
        department_id=req.department_id, requirement=req, stage=c.ApprovalStage.REQUESTER, action=c.ApprovalActionType.SUBMIT,
        from_status=RQ.DRAFT, to_status=req.status, actor=scope.user,
    )
    audit.record(scope.user, "requirement.submitted", req, request=request)
    return req


@transaction.atomic
def office_edit(scope, req: PlanRequirement, data: dict, reason: str, *, request=None) -> PlanRequirement:
    req = PlanRequirement.objects.select_for_update().get(pk=req.pk)
    _require_consolidate(scope, req.department_id)
    if req.status not in CONSOLIDATABLE:
        raise ProcurementError("This requirement is not open for consolidation.", code="invalid_status")
    if req.proposal_id and req.proposal.status not in (PS.DRAFT, PS.CONSOLIDATED):
        raise ProcurementError("The proposal has already been sent.", code="invalid_status")
    before = _snapshot(req)
    _apply_fields(req, data, req.department)
    after = _snapshot(req)
    old, new = audit.diff(before, after)
    if not new:
        raise ProcurementError("Nothing changed.", code="no_change")
    if req.status == RQ.SUBMITTED:
        req.status = RQ.UNDER_CONSOLIDATION
    req.save()
    for field in new:
        _log(req, CT.EDIT, scope.user, reason, field=field, old=old.get(field), new=new[field])
    _refresh_proposal_total(req.proposal)
    audit.record(scope.user, "requirement.office_edited", req, old=old, new=new, reason=reason, request=request)
    return req


@transaction.atomic
def remove(scope, req: PlanRequirement, reason: str, *, request=None) -> PlanRequirement:
    req = PlanRequirement.objects.select_for_update().get(pk=req.pk)
    _require_consolidate(scope, req.department_id)
    if req.status not in CONSOLIDATABLE:
        raise ProcurementError("This requirement cannot be removed now.", code="invalid_status")
    old_status, proposal = req.status, req.proposal
    req.status = RQ.REMOVED
    req.proposal = None
    req.save()
    _log(req, CT.REMOVE, scope.user, reason, field="status", old=old_status, new=req.status)
    _refresh_proposal_total(proposal)
    audit.record(scope.user, "requirement.removed", req, old={"status": old_status}, new={"status": req.status}, reason=reason, request=request)
    return req


@transaction.atomic
def restore(scope, req: PlanRequirement, reason: str, *, request=None) -> PlanRequirement:
    req = PlanRequirement.objects.select_for_update().get(pk=req.pk)
    _require_consolidate(scope, req.department_id)
    if req.status != RQ.REMOVED:
        raise ProcurementError("Only removed requirements can be restored.", code="invalid_status")
    req.status = RQ.UNDER_CONSOLIDATION
    req.save()
    _log(req, CT.RESTORE, scope.user, reason, field="status", old=RQ.REMOVED, new=req.status)
    audit.record(scope.user, "requirement.restored", req, reason=reason, request=request)
    return req


def _same_bucket(reqs) -> None:
    keys = {(r.department_id, r.financial_year, r.funding_type) for r in reqs}
    if len(keys) != 1:
        raise ProcurementError("Requirements must share department, financial year and funding type.", code="bucket_mismatch")


@transaction.atomic
def merge(scope, target: PlanRequirement, source_ids, reason: str, data: dict, *, request=None) -> PlanRequirement:
    ids = sorted({int(x) for x in (source_ids or []) if str(x).isdigit()} - {target.pk})
    if not ids:
        raise ProcurementError("Choose requirements to merge.", code="required", field="source_ids")
    locked = list(PlanRequirement.objects.select_for_update().filter(pk__in=[target.pk, *ids]).order_by("pk"))
    by_id = {r.pk: r for r in locked}
    target = by_id[target.pk]
    sources = [by_id[i] for i in ids if i in by_id]
    if len(sources) != len(ids):
        raise not_found()
    _require_consolidate(scope, target.department_id)
    _same_bucket([target, *sources])
    if any(r.status not in CONSOLIDATABLE for r in [target, *sources]):
        raise ProcurementError("Only requirements under consolidation can be merged.", code="invalid_status")
    before = _snapshot(target)
    if "quantity" not in data:
        if any(s.uom != target.uom for s in sources):
            raise ProcurementError("Units differ — give the merged quantity explicitly.", code="uom_mismatch", field="quantity")
        data = {**data, "quantity": str(target.quantity + sum(s.quantity for s in sources))}
    _apply_fields(target, data, target.department)
    if target.status == RQ.SUBMITTED:
        target.status = RQ.UNDER_CONSOLIDATION
    target.save()
    old, new = audit.diff(before, _snapshot(target))
    for field in new:
        _log(target, CT.EDIT, scope.user, reason, field=field, old=old.get(field), new=new[field])
    for s in sources:
        old_status, proposal = s.status, s.proposal
        s.status = RQ.MERGED
        s.merged_into = target
        s.proposal = None
        s.save()
        _log(s, CT.MERGE, scope.user, reason, field="merged_into", old="", new=target.number, related=target)
        _log(target, CT.MERGE, scope.user, reason, field="merged_from", new=s.number, related=s)
        _refresh_proposal_total(proposal)
    _refresh_proposal_total(target.proposal)
    audit.record(scope.user, "requirement.merged", target, new={"sources": [s.number for s in sources], **new}, reason=reason, request=request)
    return target


@transaction.atomic
def split(scope, req: PlanRequirement, parts, reason: str, *, request=None) -> list[PlanRequirement]:
    req = PlanRequirement.objects.select_for_update().get(pk=req.pk)
    _require_consolidate(scope, req.department_id)
    if req.status not in CONSOLIDATABLE:
        raise ProcurementError("Only requirements under consolidation can be split.", code="invalid_status")
    if not isinstance(parts, list) or not parts:
        raise ProcurementError("Give the quantities to split off.", code="required", field="parts")
    quantities = [parse_qty((p or {}).get("quantity"), "parts.quantity") for p in parts]
    if sum(quantities) >= req.quantity:
        raise ProcurementError("Split quantities must leave something on the original.", code="invalid_quantity")
    created = []
    for part, quantity in zip(parts, quantities):
        prefix = c.NumberPrefix.PLAN if req.funding_type == c.FundingType.PLAN else c.NumberPrefix.NON_PLAN
        new_req = PlanRequirement.objects.create(
            number=next_number(prefix, financial_year=req.financial_year), department=req.department, laboratory=req.laboratory,
            equipment=req.equipment, financial_year=req.financial_year, funding_type=req.funding_type, item=req.item,
            category=req.category, description=str((part or {}).get("description") or req.description)[:255],
            specification=req.specification, justification=req.justification, quantity=quantity, uom=req.uom,
            estimated_unit_cost=req.estimated_unit_cost, estimated_total=_total(quantity, req.estimated_unit_cost),
            priority=req.priority, status=RQ.UNDER_CONSOLIDATION, raised_by=req.raised_by, split_from=req,
            proposal=req.proposal, submitted_at=req.submitted_at, original_values={},
        )
        _log(new_req, CT.SPLIT, scope.user, reason, field="split_from", new=req.number, related=req)
        _log(req, CT.SPLIT, scope.user, reason, field="split_into", new=f"{new_req.number} ({quantity})", related=new_req)
        created.append(new_req)
    old_qty = req.quantity
    req.quantity = old_qty - sum(quantities)
    req.estimated_total = _total(req.quantity, req.estimated_unit_cost)
    if req.status == RQ.SUBMITTED:
        req.status = RQ.UNDER_CONSOLIDATION
    req.save()
    _log(req, CT.EDIT, scope.user, reason, field="quantity", old=old_qty, new=req.quantity)
    _refresh_proposal_total(req.proposal)
    audit.record(scope.user, "requirement.split", req, new={"into": [r.number for r in created]}, reason=reason, request=request)
    return created


# ---------------------------------------------------------------------------
# Proposals
# ---------------------------------------------------------------------------
def _refresh_proposal_total(p: PlanProposal | None) -> None:
    if p is None:
        return
    total = sum((r.estimated_total for r in p.requirements.exclude(status__in=[RQ.REMOVED, RQ.MERGED])), Decimal("0.00"))
    PlanProposal.objects.filter(pk=p.pk).update(total_amount=total, updated_at=timezone.now())


@transaction.atomic
def create_proposal(scope, data: dict, *, request=None) -> PlanProposal:
    dept = access.pick_department(scope, data.get("department_id"))
    access.require_config(dept)
    _require_consolidate(scope, dept.pk)
    funding = choice(data.get("funding_type"), PLAN_FUNDING, "funding_type")
    fy = str(data.get("financial_year") or "")
    if not is_valid_fy_label(fy):
        raise ProcurementError("financial_year must look like 2026-27.", code="invalid_fy", field="financial_year")
    p = PlanProposal.objects.create(
        number=next_number(c.NumberPrefix.PROPOSAL, financial_year=fy), department=dept, financial_year=fy, funding_type=funding,
        title=req_str(data, "title"), remarks=req_str(data, "remarks", max_len=5000, required=False), created_by=scope.user,
    )
    set_proposal_requirements(scope, p, data.get("requirement_ids") or [], request=request)
    audit.record(scope.user, "proposal.created", p, new={"funding_type": funding, "financial_year": fy}, request=request)
    return p


@transaction.atomic
def set_proposal_requirements(scope, p: PlanProposal, ids, *, request=None) -> PlanProposal:
    p = PlanProposal.objects.select_for_update().get(pk=p.pk)
    _require_consolidate(scope, p.department_id)
    if p.status not in (PS.DRAFT, PS.CONSOLIDATED):
        raise ProcurementError("The proposal has already been sent.", code="invalid_status")
    wanted = {int(x) for x in ids if str(x).isdigit()}
    reqs = list(
        PlanRequirement.objects.select_for_update().filter(
            pk__in=wanted, department_id=p.department_id, financial_year=p.financial_year, funding_type=p.funding_type, is_archived=False
        )
    )
    if len(reqs) != len(wanted):
        raise ProcurementError("Some requirements do not belong to this department / FY / funding type.", code="bucket_mismatch")
    for r in reqs:
        if r.status not in CONSOLIDATABLE or (r.proposal_id and r.proposal_id != p.pk):
            raise ProcurementError(f"{r.number} cannot be added to this proposal.", code="invalid_status")
    for r in p.requirements.exclude(pk__in=wanted).filter(status=RQ.CONSOLIDATED):
        r.proposal = None
        r.status = RQ.UNDER_CONSOLIDATION
        r.save(update_fields=["proposal", "status", "updated_at"])
    for r in reqs:
        r.proposal = p
        r.status = RQ.CONSOLIDATED
        r.save(update_fields=["proposal", "status", "updated_at"])
    p.status = PS.CONSOLIDATED if reqs else PS.DRAFT
    p.save(update_fields=["status", "updated_at"])
    _refresh_proposal_total(p)
    return p


@transaction.atomic
def send_proposal(scope, p: PlanProposal, *, request=None) -> PlanProposal:
    p = PlanProposal.objects.select_for_update().get(pk=p.pk)
    _require_consolidate(scope, p.department_id)
    if p.status != PS.CONSOLIDATED or not p.requirements.filter(status=RQ.CONSOLIDATED).exists():
        raise ProcurementError("Add requirements before sending the proposal.", code="invalid_status")
    p.status = PS.SENT_FOR_APPROVAL
    p.sent_at = timezone.now()
    p.save()
    p.requirements.filter(status=RQ.CONSOLIDATED).update(status=RQ.SENT_FOR_APPROVAL, updated_at=timezone.now())
    ApprovalAction.objects.create(
        department_id=p.department_id, proposal=p, stage=c.ApprovalStage.OFFICE, action=c.ApprovalActionType.SEND_FOR_APPROVAL,
        from_status=PS.CONSOLIDATED, to_status=p.status, actor=scope.user, actor_role=R.OFFICE, amount=p.total_amount,
    )
    audit.record(scope.user, "proposal.sent", p, new={"total": p.total_amount}, request=request)
    notify.notify(
        notify.hod_users(p.department_id), department_id=p.department_id, title=f"Proposal for approval: {p.number}",
        message=f"{p.title} — ₹{p.total_amount} ({p.get_funding_type_display()} {p.financial_year}).",
        link=f"/procurement/proposals/{p.pk}", event="proposal_pending", actor=scope.user, extra={"proposal_id": p.pk},
    )
    return p


@transaction.atomic
def decide_proposal(scope, p: PlanProposal, data: dict, *, upload=None, request=None) -> PlanProposal:
    p = PlanProposal.objects.select_for_update().get(pk=p.pk)
    cfg = access.require_config(p.department_id)
    if p.status != PS.SENT_FOR_APPROVAL:
        raise ProcurementError("This proposal is not waiting for a decision.", code="invalid_status")
    offline = upload is not None or str(data.get("offline") or "").lower() in ("1", "true", "yes")
    if offline:
        if cfg.hod_approval_mode == c.HodApprovalMode.IN_APP:
            raise ProcurementError("Offline approval is switched off for this department.", code="offline_disabled")
        scope.require_perm(p.department_id, P.OFFLINE_APPROVAL)
        if upload is None:
            raise ProcurementError("Attach the signed approval.", code="file_required", field="file")
    else:
        if cfg.hod_approval_mode == c.HodApprovalMode.OFFLINE or not scope.has_role(p.department_id, R.HOD):
            raise forbidden("Only the HOD can decide on this proposal.")
    reqs = list(p.requirements.filter(status=RQ.SENT_FOR_APPROVAL))
    if p.created_by_id == scope.user.pk or any(r.raised_by_id == scope.user.pk for r in reqs):
        raise forbidden("You cannot decide on a proposal you prepared or that contains your own requirement.")
    decision = choice(str(data.get("decision") or "").upper(), ("APPROVE", "REJECT"), "decision")
    comments = str(data.get("comments") or "").strip()
    if decision == "REJECT" and not comments:
        raise ProcurementError("A reason is required.", code="reason_required", field="comments")
    extra = {}
    if offline:
        from . import documents

        approval_date = parse_day(data.get("approval_date"), "approval_date", required=True)
        if approval_date > timezone.localdate():
            raise ProcurementError("The approval date cannot be in the future.", code="invalid_date", field="approval_date")
        doc = documents.create_document(
            scope, p.department, upload, doc_type=c.DocumentType.OFFLINE_APPROVAL, links={"proposal": p},
            description="Signed proposal decision", request=request,
        )
        extra = {
            "is_offline": True, "offline_approver_name": req_str(data, "approver_name"),
            "offline_approver_designation": req_str(data, "approver_designation"), "offline_approval_date": approval_date,
            "offline_reference": req_str(data, "reference", required=False), "offline_document": doc,
        }
    amounts = data.get("amounts") if isinstance(data.get("amounts"), dict) else {}
    now = timezone.now()
    if decision == "APPROVE":
        total = Decimal("0.00")
        for r in reqs:
            raw = amounts.get(str(r.pk))
            amount = parse_money(raw, f"amounts.{r.pk}") if raw not in (None, "") else r.estimated_total
            if amount > r.estimated_total:
                raise ProcurementError(f"Approved amount for {r.number} exceeds its estimate.", code="invalid_amount")
            r.approved_amount = amount
            r.status = RQ.APPROVED if amount > 0 else RQ.REJECTED
            r.save(update_fields=["approved_amount", "status", "updated_at"])
            if amount > 0:
                total += amount
        p.status = PS.APPROVED
        p.approved_amount = total
    else:
        for r in reqs:
            r.status = RQ.REJECTED
            r.save(update_fields=["status", "updated_at"])
        p.status = PS.REJECTED
    p.decided_at = now
    p.save()
    action = {
        ("APPROVE", False): c.ApprovalActionType.APPROVE, ("APPROVE", True): c.ApprovalActionType.OFFLINE_APPROVE,
        ("REJECT", False): c.ApprovalActionType.REJECT, ("REJECT", True): c.ApprovalActionType.OFFLINE_REJECT,
    }[(decision, offline)]
    ApprovalAction.objects.create(
        department_id=p.department_id, proposal=p, stage=c.ApprovalStage.HOD, action=action, from_status=PS.SENT_FOR_APPROVAL,
        to_status=p.status, actor=scope.user, actor_role=R.OFFICE if offline else R.HOD, comments=comments,
        amount=p.approved_amount, **extra,
    )
    audit.record(scope.user, f"proposal.{decision.lower()}d", p, new={"status": p.status, "approved": p.approved_amount},
                 reason=comments, request=request)
    notify.notify(
        [p.created_by, *{r.raised_by for r in reqs}], department_id=p.department_id, title=f"Proposal {p.number}: {p.get_status_display()}",
        message=f"{p.title} — {p.get_status_display()}.", link=f"/procurement/proposals/{p.pk}", event="proposal_decided",
        actor=scope.user, extra={"proposal_id": p.pk},
    )
    return p
