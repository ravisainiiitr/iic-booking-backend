"""Server-side request approval state machine.

Route (fixed at submission, stored on the request):
  1. OIC         — when the request type requires it and the request is for an equipment, unless the requester is
                   that equipment's OIC (OIC-raised requests skip the stage).
  2. OC Stores   — when the request type requires it. With ``stores_issue_flow`` Stores may mark the request
                   available (then issue from stock), partially available (issue now, the rest continues) or not
                   available (continues to purchase).
  3. Accounts    — optional budget-availability check by the Accounts In Charge (``accounts_budget_check``).
  4. HOD         — request type rule ALWAYS, ABOVE_THRESHOLD (estimated total > configured HOD threshold) or the
                   category's ``hod_required_always``. Recorded in-app by the HOD, or offline by Office (with the
                   ``offline_approval`` permission) against an uploaded signed document — per ``hod_approval_mode``.

Invariants: every transition locks the row and re-checks status; the requester can never act on any approval
stage of their own request; hold and reject need a reason; each transition writes an immutable
``ApprovalAction`` plus an audit entry.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import access, audit, notify
from . import constants as c
from .errors import ProcurementError, forbidden
from .models import ApprovalAction, PurchaseRequest, PurchaseRequestLine
from .requests_service import validate_for_submit

S = c.ApprovalStage
RS = c.RequestStatus
A = c.ApprovalActionType
R = c.ModuleRole
P = c.OfficePermission

CANCELLABLE = frozenset(
    {RS.DRAFT, RS.PENDING_OIC, RS.PENDING_STORES, RS.PENDING_HOD, RS.ON_HOLD, RS.REJECTED, RS.APPROVED, RS.STORES_AVAILABLE}
)
STAGE_ROLE = {S.OIC: R.OIC, S.STORES: R.OC_STORES, S.ACCOUNTS: R.ACCOUNTS, S.HOD: R.HOD}


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------
def hod_required(r: PurchaseRequest, cfg) -> bool:
    if r.category_id and r.category.hod_required_always:
        return True
    rule = r.request_type.hod_rule
    if rule == c.HodRule.ALWAYS:
        return True
    if rule == c.HodRule.ABOVE_THRESHOLD:
        return r.estimated_total > cfg.hod_approval_threshold
    return False


def compute_route(r: PurchaseRequest, cfg) -> list[str]:
    route = []
    if r.request_type.requires_oic and r.equipment_id and r.raised_as_role != R.OIC:
        route.append(S.OIC.value)
    if r.request_type.requires_stores:
        route.append(S.STORES.value)
    if cfg.accounts_budget_check:
        route.append(S.ACCOUNTS.value)
    if hod_required(r, cfg):
        route.append(S.HOD.value)
    return route


def _offline_recorders(r) -> list:
    return [u for u in notify.office_users(r.department_id, P.OFFLINE_APPROVAL) if u.pk != r.requested_by_id]


def _ensure_approvers(r, cfg, route) -> None:
    for stage in route:
        users = notify.stage_approvers(r, stage) if not (stage == S.HOD and cfg.hod_approval_mode == c.HodApprovalMode.OFFLINE) else []
        if stage == S.HOD and cfg.hod_approval_mode != c.HodApprovalMode.IN_APP:
            users = users + _offline_recorders(r)
        if not users:
            raise ProcurementError(
                f"No {S(stage).label} is available to approve this request. Ask the Main Administrator to assign one.",
                code="no_approver",
                stage=stage,
            )


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------
def can_act_on_stage(scope, r: PurchaseRequest, stage: str, cfg) -> bool:
    if scope.user.pk == r.requested_by_id:
        return False
    if stage == S.OIC:
        return scope.is_oic_for(r.equipment_id)
    if stage == S.STORES:
        return scope.has_role(r.department_id, R.OC_STORES)
    if stage == S.ACCOUNTS:
        return scope.has_role(r.department_id, R.ACCOUNTS)
    if stage == S.HOD:
        return scope.has_role(r.department_id, R.HOD) and cfg.hod_approval_mode != c.HodApprovalMode.OFFLINE
    return False


def can_record_offline(scope, r: PurchaseRequest, cfg) -> bool:
    return (
        r.status == RS.PENDING_HOD
        and r.current_stage == S.HOD
        and scope.user.pk != r.requested_by_id
        and cfg.hod_approval_mode != c.HodApprovalMode.IN_APP
        and scope.has_perm(r.department_id, P.OFFLINE_APPROVAL)
    )


def _held_stage(r) -> str:
    route = r.approval_route or []
    return route[r.route_index] if r.route_index < len(route) else ""


def _require_stage(scope, r, cfg, stage) -> None:
    if scope.user.pk == r.requested_by_id:
        raise forbidden("You cannot approve or decide on your own request.")
    if not can_act_on_stage(scope, r, stage, cfg):
        raise forbidden(f"Only the {S(stage).label} can act at this stage.")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _lock(r) -> PurchaseRequest:
    return (
        PurchaseRequest.objects.select_for_update(of=("self",))
        .select_related("department", "request_type", "category", "equipment", "requested_by")
        .get(pk=r.pk)
    )


def _act(r, scope, *, stage, action, from_status, comments="", amount=None, request=None, **offline) -> ApprovalAction:
    from iic_booking.users.mobile_sessions import client_ip

    if r.status != from_status:
        r.stage_entered_at = timezone.now()
        PurchaseRequest.objects.filter(pk=r.pk).update(stage_entered_at=r.stage_entered_at)

    roles = scope.roles(r.department_id)
    actor_role = STAGE_ROLE.get(stage, "") if STAGE_ROLE.get(stage) in roles else ""
    if not actor_role and stage == S.REQUESTER:
        actor_role = r.raised_as_role
    if not actor_role and offline.get("is_offline"):
        actor_role = R.OFFICE
    if not actor_role and R.MAIN_ADMIN in roles:
        actor_role = R.MAIN_ADMIN
    row = ApprovalAction.objects.create(
        department_id=r.department_id,
        purchase_request=r,
        stage=stage,
        action=action,
        from_status=from_status,
        to_status=r.status,
        actor=scope.user,
        actor_role=actor_role,
        comments=comments or "",
        amount=amount,
        ip_address=client_ip(request) if request is not None else None,
        **offline,
    )
    audit.record(
        scope.user,
        f"request.{action.lower()}",
        r,
        old={"status": from_status},
        new={"status": r.status, "stage": stage, "amount": amount, "route_index": r.route_index},
        reason=comments or "",
        request=request,
    )
    return row


def _advance(r, scope, cfg, *, stage, action, comments="", amount=None, request=None, **offline) -> None:
    from_status = r.status
    if amount is not None:
        r.approved_amount = amount
    r.route_index += 1
    route = r.approval_route or []
    if r.route_index < len(route):
        r.status = c.STAGE_STATUS[route[r.route_index]]
        r.save()
        _act(r, scope, stage=stage, action=action, from_status=from_status, comments=comments, amount=amount, request=request, **offline)
        notify.request_pending(r, route[r.route_index], scope.user)
        return
    r.status = RS.APPROVED
    r.approved_at = timezone.now()
    if r.approved_amount is None:
        r.approved_amount = r.estimated_total
    r.save()
    _act(r, scope, stage=stage, action=action, from_status=from_status, comments=comments, amount=amount, request=request, **offline)
    notify.request_update(r, A.APPROVE, scope.user)


def _check_amount(r, amount) -> None:
    if amount is None:
        return
    cap = r.approved_amount if r.approved_amount is not None else r.estimated_total
    if amount <= 0 or amount > cap:
        raise ProcurementError(
            f"The approved amount must be more than zero and not exceed ₹{cap}.", code="invalid_amount", field="amount"
        )


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------
@transaction.atomic
def submit(scope, r: PurchaseRequest, *, comments: str = "", request=None) -> PurchaseRequest:
    from .small_purchase import evaluate

    r = _lock(r)
    if r.requested_by_id != scope.user.pk:
        raise forbidden("Only the requester can submit this request.")
    cfg = access.require_config(r.department)
    from_status = r.status
    if r.status == RS.DRAFT:
        action = A.SUBMIT
    elif r.status == RS.REJECTED:
        if not cfg.allow_resubmission:
            raise ProcurementError("Resubmission is switched off for this department.", code="resubmission_disabled")
        action = A.RESUBMIT
        r.resubmission_count += 1
    else:
        raise ProcurementError("Only drafts and rejected requests can be submitted.", code="invalid_status")
    validate_for_submit(r, cfg)
    r.is_small_purchase = evaluate(cfg, r.estimated_total, category=r.category, request_type=r.request_type).eligible
    r.hod_required = hod_required(r, cfg)
    route = compute_route(r, cfg)
    _ensure_approvers(r, cfg, route)
    r.approval_route = route
    r.route_index = 0
    r.held_from_status = ""
    r.last_reason = ""
    r.approved_amount = None
    r.approved_at = None
    r.submitted_at = timezone.now()
    if route:
        r.status = c.STAGE_STATUS[route[0]]
    else:
        r.status = RS.APPROVED
        r.approved_at = timezone.now()
        r.approved_amount = r.estimated_total
    r.save()
    _act(r, scope, stage=S.REQUESTER, action=action, from_status=from_status, comments=comments, request=request)
    if route:
        notify.request_pending(r, route[0], scope.user)
    return r


@transaction.atomic
def approve(scope, r: PurchaseRequest, *, comments: str = "", amount: Decimal | None = None, request=None) -> PurchaseRequest:
    r = _lock(r)
    cfg = access.require_config(r.department)
    stage = r.current_stage
    if not stage:
        raise ProcurementError("This request is not waiting for approval.", code="invalid_status")
    _require_stage(scope, r, cfg, stage)
    _check_amount(r, amount)
    _advance(r, scope, cfg, stage=stage, action=A.APPROVE, comments=comments, amount=amount, request=request)
    return r


@transaction.atomic
def reject(scope, r: PurchaseRequest, *, reason: str, request=None) -> PurchaseRequest:
    r = _lock(r)
    cfg = access.require_config(r.department)
    stage = r.current_stage
    if not stage:
        raise ProcurementError("This request is not waiting for approval.", code="invalid_status")
    _require_stage(scope, r, cfg, stage)
    from_status = r.status
    r.status = RS.REJECTED
    r.last_reason = reason
    r.save()
    _act(r, scope, stage=stage, action=A.REJECT, from_status=from_status, comments=reason, request=request)
    notify.request_update(r, A.REJECT, scope.user, reason)
    return r


@transaction.atomic
def hold(scope, r: PurchaseRequest, *, reason: str, request=None) -> PurchaseRequest:
    r = _lock(r)
    cfg = access.require_config(r.department)
    stage = r.current_stage
    if not stage:
        raise ProcurementError("Only requests waiting for approval can be put on hold.", code="invalid_status")
    _require_stage(scope, r, cfg, stage)
    from_status = r.status
    r.held_from_status = r.status
    r.status = RS.ON_HOLD
    r.last_reason = reason
    r.save()
    _act(r, scope, stage=stage, action=A.HOLD, from_status=from_status, comments=reason, request=request)
    notify.request_update(r, A.HOLD, scope.user, reason)
    return r


@transaction.atomic
def resume(scope, r: PurchaseRequest, *, comments: str = "", request=None) -> PurchaseRequest:
    r = _lock(r)
    cfg = access.require_config(r.department)
    if r.status != RS.ON_HOLD or not r.held_from_status:
        raise ProcurementError("This request is not on hold.", code="invalid_status")
    stage = _held_stage(r)
    _require_stage(scope, r, cfg, stage)
    from_status = r.status
    r.status = r.held_from_status
    r.held_from_status = ""
    r.save()
    _act(r, scope, stage=stage, action=A.RESUME, from_status=from_status, comments=comments, request=request)
    notify.request_update(r, A.RESUME, scope.user)
    return r


@transaction.atomic
def cancel(scope, r: PurchaseRequest, *, reason: str, request=None) -> PurchaseRequest:
    r = _lock(r)
    access.require_config(r.department)
    is_requester = r.requested_by_id == scope.user.pk
    if not (is_requester or scope.has_role(r.department_id, R.MAIN_ADMIN)):
        raise forbidden("Only the requester or the Main Administrator can cancel this request.")
    if r.status not in CANCELLABLE:
        raise ProcurementError("This request can no longer be cancelled.", code="invalid_status")
    from_status = r.status
    r.status = RS.CANCELLED
    r.last_reason = reason
    r.held_from_status = ""
    r.save()
    _act(
        r, scope, stage=S.REQUESTER if is_requester else S.SYSTEM, action=A.CANCEL, from_status=from_status,
        comments=reason, request=request,
    )
    if not is_requester:
        notify.request_update(r, A.CANCEL, scope.user, reason)
    return r


def _remaining_total(r) -> Decimal:
    total = Decimal("0.00")
    for line in r.lines.all():
        if line.quantity <= 0:
            continue
        remaining = line.quantity - line.issued_quantity
        total += (line.line_total * remaining / line.quantity).quantize(Decimal("0.01"))
    return total


def _issue_lines(scope, r, quantities: dict[int, Decimal], request=None) -> list[PurchaseRequestLine]:
    issued = []
    for line in r.lines.select_for_update():
        q = quantities.get(line.pk)
        if not q:
            continue
        line.issued_quantity = line.issued_quantity + q
        line.save(update_fields=["issued_quantity", "updated_at"])
        issued.append(line)
    from .stock import issue_for_request

    issue_for_request(scope, r, issued, quantities, request=request)
    return issued


@transaction.atomic
def stores_review(scope, r: PurchaseRequest, *, decision: str, lines=None, comments: str = "", request=None):
    from .api import parse_int, parse_qty

    r = _lock(r)
    cfg = access.require_config(r.department)
    if r.current_stage != S.STORES:
        raise ProcurementError("This request is not with OC Stores.", code="invalid_status")
    _require_stage(scope, r, cfg, S.STORES)
    if not r.request_type.stores_issue_flow:
        raise ProcurementError("Availability review does not apply to this request type.", code="not_applicable")
    if decision == "BY_LINES":
        open_lines = [line for line in r.lines.all() if line.quantity > line.issued_quantity]
        stock = [line for line in open_lines if line.fulfilment == c.LineFulfilment.STOCK]
        if len(stock) == len(open_lines):
            decision = "AVAILABLE"
        elif not stock:
            decision = "NOT_AVAILABLE"
        else:
            decision = "PARTIAL"
            lines = [{"line_id": line.pk, "quantity": str(line.quantity - line.issued_quantity)} for line in stock]
    from_status = r.status
    if decision == "AVAILABLE":
        r.status = RS.STORES_AVAILABLE
        r.save()
        _act(r, scope, stage=S.STORES, action=A.STORES_AVAILABLE, from_status=from_status, comments=comments, request=request)
        notify.request_update(r, A.STORES_AVAILABLE, scope.user)
        return r
    if decision == "NOT_AVAILABLE":
        _advance(r, scope, cfg, stage=S.STORES, action=A.STORES_NOT_AVAILABLE, comments=comments, request=request)
        return r
    if decision != "PARTIAL":
        raise ProcurementError(
            "decision must be AVAILABLE, PARTIAL, NOT_AVAILABLE or BY_LINES.", code="invalid_choice", field="decision"
        )
    if not isinstance(lines, list) or not lines:
        raise ProcurementError("List the quantities available for each line.", code="lines_required", field="lines")
    by_id = {line.pk: line for line in r.lines.all()}
    quantities: dict[int, Decimal] = {}
    for raw in lines:
        lid = parse_int((raw or {}).get("line_id"), "line_id", required=True)
        if lid not in by_id:
            raise ProcurementError("Unknown line.", code="invalid", field="lines")
        q = parse_qty((raw or {}).get("quantity"), "quantity")
        if q > by_id[lid].quantity - by_id[lid].issued_quantity:
            raise ProcurementError("Available quantity exceeds the requested quantity.", code="invalid_quantity", field="lines")
        quantities[lid] = q
    full = all(quantities.get(lid, 0) == line.quantity - line.issued_quantity for lid, line in by_id.items())
    if full:
        raise ProcurementError("Everything is available — mark the request as available instead.", code="use_available")
    _issue_lines(scope, r, quantities, request=request)
    summary = ", ".join(f"{by_id[lid].description}: {q}" for lid, q in quantities.items())
    r.approved_amount = _remaining_total(r)
    _advance(
        r, scope, cfg, stage=S.STORES, action=A.STORES_PARTIAL,
        comments=(f"Issued from stores — {summary}. " + (comments or "")).strip(), request=request,
    )
    return r


@transaction.atomic
def issue(scope, r: PurchaseRequest, *, comments: str = "", request=None) -> PurchaseRequest:
    r = _lock(r)
    access.require_config(r.department)
    if r.status != RS.STORES_AVAILABLE:
        raise ProcurementError("Only requests marked available can be issued.", code="invalid_status")
    if scope.user.pk == r.requested_by_id:
        raise forbidden("You cannot issue stock against your own request.")
    if not scope.has_role(r.department_id, R.OC_STORES):
        raise forbidden("Only OC Stores can issue stock.")
    quantities = {line.pk: line.quantity - line.issued_quantity for line in r.lines.all() if line.quantity > line.issued_quantity}
    _issue_lines(scope, r, quantities, request=request)
    from_status = r.status
    r.status = RS.ISSUED
    r.completed_at = timezone.now()
    r.save()
    _act(r, scope, stage=S.STORES, action=A.ISSUE, from_status=from_status, comments=comments, request=request)
    notify.request_update(r, A.ISSUE, scope.user)
    return r


@transaction.atomic
def offline_hod_decision(
    scope,
    r: PurchaseRequest,
    *,
    decision: str,
    upload,
    approver_name: str,
    approver_designation: str,
    approval_date,
    reference: str = "",
    comments: str = "",
    amount: Decimal | None = None,
    request=None,
) -> PurchaseRequest:
    from . import documents

    r = _lock(r)
    cfg = access.require_config(r.department)
    if scope.user.pk == r.requested_by_id:
        raise forbidden("You cannot record a decision on your own request.")
    if not can_record_offline(scope, r, cfg):
        if r.status != RS.PENDING_HOD:
            raise ProcurementError("This request is not waiting for HOD approval.", code="invalid_status")
        if cfg.hod_approval_mode == c.HodApprovalMode.IN_APP:
            raise ProcurementError("Offline HOD approval is switched off for this department.", code="offline_disabled")
        raise forbidden("You do not have permission to record offline approvals.")
    if decision not in ("APPROVE", "REJECT"):
        raise ProcurementError("decision must be APPROVE or REJECT.", code="invalid_choice", field="decision")
    if approval_date is None:
        raise ProcurementError("approval_date is required.", code="required", field="approval_date")
    if approval_date > timezone.localdate():
        raise ProcurementError("The approval date cannot be in the future.", code="invalid_date", field="approval_date")
    if r.submitted_at and approval_date < timezone.localtime(r.submitted_at).date():
        raise ProcurementError("The approval date is before the request was submitted.", code="invalid_date", field="approval_date")
    if decision == "REJECT" and not comments.strip():
        raise ProcurementError("A reason is required.", code="reason_required", field="comments")
    _check_amount(r, amount)
    doc = documents.create_document(
        scope, r.department, upload, doc_type=c.DocumentType.OFFLINE_APPROVAL, links={"purchase_request": r},
        description=f"Signed HOD decision — {approver_name}", request=request,
    )
    offline = {
        "is_offline": True,
        "offline_approver_name": approver_name,
        "offline_approver_designation": approver_designation,
        "offline_approval_date": approval_date,
        "offline_reference": reference,
        "offline_document": doc,
    }
    if decision == "APPROVE":
        _advance(r, scope, cfg, stage=S.HOD, action=A.OFFLINE_APPROVE, comments=comments, amount=amount, request=request, **offline)
        return r
    from_status = r.status
    r.status = RS.REJECTED
    r.last_reason = comments
    r.save()
    _act(r, scope, stage=S.HOD, action=A.OFFLINE_REJECT, from_status=from_status, comments=comments, request=request, **offline)
    notify.request_update(r, A.OFFLINE_REJECT, scope.user, comments)
    return r


# ---------------------------------------------------------------------------
# Queries for the UI
# ---------------------------------------------------------------------------
def available_actions(r: PurchaseRequest, scope) -> list[str]:
    cfg = access.get_config(r.department_id)
    if cfg is None or not cfg.module_enabled:
        return []
    out: list[str] = []
    mine = r.requested_by_id == scope.user.pk
    if mine and r.status == RS.DRAFT:
        out += ["edit", "submit"]
    if mine and r.status == RS.REJECTED and cfg.allow_resubmission:
        out += ["edit", "resubmit"]
    if r.status in CANCELLABLE and (mine or scope.has_role(r.department_id, R.MAIN_ADMIN)):
        out.append("cancel")
    stage = r.current_stage
    if stage and can_act_on_stage(scope, r, stage, cfg):
        out += ["approve", "reject", "hold"]
        if stage == S.STORES and r.request_type.stores_issue_flow:
            out.append("stores_review")
        if stage == S.STORES:
            out.append("stores_edit")
    if r.status == RS.ON_HOLD and can_act_on_stage(scope, r, _held_stage(r), cfg):
        out.append("resume")
    if can_record_offline(scope, r, cfg):
        out.append("offline_hod_decision")
    if r.status == RS.STORES_AVAILABLE and not mine and scope.has_role(r.department_id, R.OC_STORES):
        out.append("issue")
    if r.status == RS.APPROVED and scope.has_perm(r.department_id, P.PROCUREMENT):
        out.append("start_procurement")
    if r.status == RS.APPROVED and r.is_small_purchase and scope.has_perm(r.department_id, P.RECORD_SMALL_PURCHASE):
        out.append("record_purchase")
    return out


def pending_for_me_q(scope) -> Q:
    """Requests waiting on this user (approval inbox). Never includes the user's own requests."""
    q = Q(pk__in=[])
    oic_eq = list(scope.oic_equipment)
    stores = [d for d in scope.department_ids() if scope.has_role(d, R.OC_STORES)]
    accounts = [d for d in scope.department_ids() if scope.has_role(d, R.ACCOUNTS)]
    hod = [d for d in scope.department_ids() if scope.has_role(d, R.HOD)]
    offline = [d for d in scope.department_ids() if scope.has_perm(d, P.OFFLINE_APPROVAL)]
    enabled = list(scope.department_ids())

    def at(status):
        return Q(status=status) | Q(status=RS.ON_HOLD, held_from_status=status)

    if oic_eq:
        q |= at(RS.PENDING_OIC) & Q(equipment_id__in=oic_eq)
    if stores:
        q |= (at(RS.PENDING_STORES) | Q(status=RS.STORES_AVAILABLE)) & Q(department_id__in=stores)
    if accounts:
        q |= at(RS.PENDING_ACCOUNTS) & Q(department_id__in=accounts)
    if hod or offline:
        q |= at(RS.PENDING_HOD) & Q(department_id__in=list(set(hod) | set(offline)))
    return q & Q(department_id__in=enabled) & ~Q(requested_by=scope.user)
