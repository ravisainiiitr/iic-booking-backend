"""Services for administrator-controlled Wallet Credit Facility."""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import SubWallet, Wallet
from iic_booking.users.models.wallet_credit_facility import (
    ACTIVE_CREDIT_BLOCKING_STATUSES,
    WalletCreditAuditEvent,
    WalletCreditFacility,
    WalletCreditFacilityStatus,
    WalletCreditInvoice,
    WalletCreditInvoiceStatus,
    WalletCreditLedgerEntry,
    WalletCreditLedgerKind,
    WalletCreditPayment,
    WalletCreditPolicy,
)

logger = logging.getLogger(__name__)
TWO = Decimal("0.01")

STUDENT_TYPES = frozenset({UserType.STUDENT, UserType.INDIVIDUAL_STUDENT})
# Eligible for requesting credit: non-student, non-external internal faculty/staff.
# Authoritative gate is UserEligibilityService.can_request_wallet_credit (classifications).
ELIGIBLE_CREDIT_REQUEST_TYPES = frozenset(
    {
        UserType.FACULTY,
        UserType.ADMIN,
        UserType.DEPT_ADMIN,
        UserType.MANAGER,
        UserType.OPERATOR,
        UserType.FINANCE,
        UserType.EXTERNAL_RELATIONS,
        UserType.ORG_ADMIN,
    }
)


class WalletCreditError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status
        super().__init__(message)


def feature_enabled() -> bool:
    from iic_booking.users.identity.flags import wallet_credit_enabled

    if not wallet_credit_enabled():
        return False
    return bool(WalletCreditPolicy.get_solo().enabled)


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(TWO, rounding=ROUND_HALF_UP)


def _na(value) -> str:
    if value is None:
        return "Not available"
    text = str(value).strip()
    return text if text else "Not available"


def build_profile_snapshot(user) -> dict[str, Any]:
    dept = getattr(user, "department", None)
    from iic_booking.users.identity.service import UserIdentityService

    view = UserIdentityService.view(user)
    profile = UserIdentityService.get_profile(user)
    return {
        "name": _na(getattr(user, "name", None)),
        "email": _na(getattr(user, "email", None)),
        "employee_id": _na(getattr(user, "emp_id", None)),
        "channel_i_user_id": view.channel_i_user_id,
        "channel_i_username": view.channel_i_username,
        "user_type": view.classification,
        "portal_user_type": _na(getattr(user, "user_type", None)),
        "degree": view.degree_name,
        "channel_i_department": view.channel_i_department_name,
        "internal_department": view.internal_department_name,
        "department": _na(getattr(dept, "name", None) if dept else None),
        "designation": _na(getattr(user, "designation", None)),
        "date_of_joining": _na(getattr(user, "joining_date", None)),
        "student_start_date": _na(view.student_start_date),
        "channel_i_end_date": _na(view.validity.channel_i_end_date),
        "derived_end_date": _na(view.validity.derived_end_date),
        "validity_source": view.validity.validity_source,
        "account_status": "disabled" if not getattr(user, "is_active", True) else "active",
        "date_of_birth": _na(getattr(user, "date_of_birth", None)),
        "mobile": _na(getattr(user, "phone_number", None)),
        "account_created_at": _na(getattr(user, "date_joined", None)),
        "last_login": _na(getattr(user, "last_login", None)),
        "source": {
            "employee_id": "Portal (Channel-I mapped field)",
            "channel_i_user_id": "Channel-I",
            "channel_i_username": "Channel-I",
            "user_type": "Normalized classification (not Django role)",
            "degree": "Channel-I",
            "channel_i_department": "Channel-I",
            "internal_department": "Portal mapping",
            "designation": "Channel-I / Portal",
            "student_start_date": "Channel-I" if view.student_start_date else "Not available",
            "channel_i_end_date": "Channel-I" if view.validity.channel_i_end_date else "Not available",
            "derived_end_date": "Portal derived" if view.validity.derived_end_date else "Not available",
        },
    }


def assert_user_may_request_credit(user) -> None:
    from iic_booking.users.identity.service import UserEligibilityService

    ok, code, message = UserEligibilityService.can_request_wallet_credit(user)
    if not ok:
        raise WalletCreditError(code, message, status=403)


def user_has_blocking_facility(user) -> WalletCreditFacility | None:
    return (
        WalletCreditFacility.objects.filter(user=user, status__in=ACTIVE_CREDIT_BLOCKING_STATUSES)
        .order_by("-id")
        .first()
    )


def next_public_reference() -> str:
    year = timezone.now().year
    prefix = f"WC-{year}-"
    last = (
        WalletCreditFacility.objects.filter(public_reference__startswith=prefix)
        .order_by("-public_reference")
        .values_list("public_reference", flat=True)
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(str(last).split("-")[-1]) + 1
        except ValueError:
            seq = WalletCreditFacility.objects.filter(public_reference__startswith=prefix).count() + 1
    return f"{prefix}{seq:06d}"


def next_invoice_number() -> str:
    year = timezone.now().year
    prefix = f"WCI-{year}-"
    last = (
        WalletCreditInvoice.objects.filter(invoice_number__startswith=prefix)
        .order_by("-invoice_number")
        .values_list("invoice_number", flat=True)
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(str(last).split("-")[-1]) + 1
        except ValueError:
            seq = WalletCreditInvoice.objects.filter(invoice_number__startswith=prefix).count() + 1
    return f"{prefix}{seq:06d}"


def next_receipt_number() -> str:
    year = timezone.now().year
    prefix = f"WCR-{year}-"
    last = (
        WalletCreditPayment.objects.filter(receipt_number__startswith=prefix)
        .order_by("-receipt_number")
        .values_list("receipt_number", flat=True)
        .first()
    )
    seq = 1
    if last:
        try:
            seq = int(str(last).split("-")[-1]) + 1
        except ValueError:
            seq = WalletCreditPayment.objects.filter(receipt_number__startswith=prefix).count() + 1
    return f"{prefix}{seq:06d}"


def next_ledger_reference(kind: str) -> str:
    return f"{kind}-{timezone.now().strftime('%Y%m%d%H%M%S%f')}"


def audit(
    facility: WalletCreditFacility,
    *,
    actor,
    action: str,
    previous: str = "",
    new: str = "",
    reason: str = "",
    metadata: dict | None = None,
) -> None:
    WalletCreditAuditEvent.objects.create(
        facility=facility,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        action=action,
        previous_value=previous or "",
        new_value=new or "",
        reason=reason or "",
        metadata=metadata or {},
    )


def recompute_outstanding(facility: WalletCreditFacility) -> Decimal:
    credited = (
        facility.ledger_entries.filter(kind=WalletCreditLedgerKind.WALLET_CREDIT).aggregate(s=Sum("amount"))["s"]
        or 0
    )
    repaid = (
        facility.ledger_entries.filter(kind=WalletCreditLedgerKind.CREDIT_REPAYMENT).aggregate(s=Sum("amount"))["s"]
        or 0
    )
    adjustments = (
        facility.ledger_entries.filter(kind=WalletCreditLedgerKind.CREDIT_ADJUSTMENT).aggregate(s=Sum("amount"))["s"]
        or 0
    )
    reversals = (
        facility.ledger_entries.filter(kind=WalletCreditLedgerKind.CREDIT_REVERSAL).aggregate(s=Sum("amount"))["s"]
        or 0
    )
    outstanding = money(credited) - money(repaid) + money(adjustments) - money(reversals)
    if outstanding < 0:
        outstanding = Decimal("0.00")
    facility.outstanding_amount = outstanding
    return outstanding


def list_eligible_credit_departments(user) -> list[dict[str, Any]]:
    """Departments the user may request credit for (admin-enabled + user wallet link)."""
    from iic_booking.users.models.department import Department, DepartmentType

    wallet, _ = Wallet.objects.get_or_create(user=user)
    enabled = {
        d.id: d
        for d in Department.objects.filter(
            enable_wallet_credit=True,
            department_type=DepartmentType.INTERNAL,
        )
    }
    if not enabled:
        return []

    rows: list[dict[str, Any]] = []
    seen: set[int] = set()
    for sw in (
        SubWallet.objects.filter(wallet=wallet, department_id__in=enabled.keys())
        .select_related("department")
        .order_by("department__name")
    ):
        dept = sw.department
        if not dept or dept.id in seen:
            continue
        seen.add(dept.id)
        rows.append(
            {
                "id": dept.id,
                "name": dept.name,
                "code": dept.code or "",
                "balance": str(money(sw.balance)),
            }
        )

    home_id = getattr(user, "department_id", None)
    if home_id and home_id in enabled and home_id not in seen:
        dept = enabled[home_id]
        rows.append(
            {
                "id": dept.id,
                "name": dept.name,
                "code": dept.code or "",
                "balance": "0.00",
            }
        )
        seen.add(home_id)

    rows.sort(key=lambda r: (r["name"] or "").lower())
    return rows


def resolve_subwallet_for_user(user, department_id: int | None = None) -> SubWallet:
    from iic_booking.users.models.department import Department, DepartmentType

    wallet, _ = Wallet.objects.get_or_create(user=user)
    qs = SubWallet.objects.filter(wallet=wallet).select_related("department")
    if not department_id:
        raise WalletCreditError(
            "DEPARTMENT_REQUIRED",
            "Select the department for which you want to raise the credit request.",
        )

    dept = (
        Department.objects.filter(
            pk=department_id,
            department_type=DepartmentType.INTERNAL,
        )
        .first()
    )
    if not dept:
        raise WalletCreditError("DEPARTMENT_NOT_FOUND", "Selected department was not found.")
    if not bool(getattr(dept, "enable_wallet_credit", False)):
        raise WalletCreditError(
            "CREDIT_NOT_ENABLED_FOR_DEPARTMENT",
            "Wallet Credit Facility is not enabled for the selected department. Contact the Main Administrator.",
            status=403,
        )

    sub = qs.filter(department_id=department_id).first()
    if sub:
        return sub
    return SubWallet.objects.create(wallet=wallet, department=dept, balance=Decimal("0.00"))


def build_admin_review_context(facility: WalletCreditFacility) -> dict[str, Any]:
    """Decision-support payload for Main Admin review."""
    user = facility.user
    wallet = Wallet.objects.filter(user=user).first()
    total_balance = money(wallet.total_balance) if wallet else Decimal("0.00")
    sub_rows = []
    if wallet:
        for sw in SubWallet.objects.filter(wallet=wallet).select_related("department").order_by("department__name"):
            sub_rows.append(
                {
                    "department_id": sw.department_id,
                    "department_name": sw.department.name if sw.department_id else "",
                    "balance": str(money(sw.balance)),
                }
            )
    past = (
        WalletCreditFacility.objects.filter(user=user)
        .exclude(pk=facility.pk)
        .order_by("-created_at")[:25]
    )
    past_credits = [
        {
            "id": f.id,
            "public_reference": f.public_reference,
            "status": f.status,
            "requested_amount": str(money(f.requested_amount)),
            "approved_amount": str(money(f.approved_amount)) if f.approved_amount is not None else None,
            "outstanding_amount": str(money(f.outstanding_amount)),
            "department_name": f.department.name if f.department_id else "",
            "due_date": f.due_date.isoformat() if f.due_date else None,
            "credited_at": f.credited_at.isoformat() if f.credited_at else None,
            "cleared_at": f.cleared_at.isoformat() if f.cleared_at else None,
            "requested_at": f.requested_at.isoformat() if f.requested_at else None,
        }
        for f in past
    ]
    total_credited = (
        WalletCreditLedgerEntry.objects.filter(
            facility__user=user,
            kind=WalletCreditLedgerKind.WALLET_CREDIT,
        ).aggregate(s=Sum("amount"))["s"]
        or 0
    )
    return {
        "current_wallet_balance": str(total_balance),
        "sub_wallets": sub_rows,
        "request_department": facility.department.name if facility.department_id else "",
        "request_subwallet_balance": str(money(facility.sub_wallet.balance))
        if facility.sub_wallet_id
        else "0.00",
        "past_credit_facilities": past_credits,
        "lifetime_credit_posted": str(money(total_credited)),
        "date_of_joining": _na(getattr(user, "joining_date", None)),
        "employee_id": _na(getattr(user, "emp_id", None)),
        "designation": _na(getattr(user, "designation", None)),
        "user_type": _na(getattr(user, "user_type", None)),
        "user_email": _na(getattr(user, "email", None)),
        "user_name": _na(getattr(user, "name", None)),
        "mobile": _na(getattr(user, "phone_number", None)),
    }


def notify_user_credit_approved(facility: WalletCreditFacility) -> None:
    """Email + best-effort push when Main Admin approves a credit request."""
    try:
        from iic_booking.communication.service import CommunicationService
        from iic_booking.communication.utils import get_frontend_absolute_url

        user = facility.user
        dept_name = facility.department.name if facility.department_id else ""
        ctx = {
            "user_name": getattr(user, "name", None) or getattr(user, "email", "") or "User",
            "user_email": getattr(user, "email", "") or "",
            "public_reference": facility.public_reference,
            "requested_amount": str(money(facility.requested_amount)),
            "approved_amount": str(money(facility.approved_amount or 0)),
            "due_date": facility.due_date.isoformat() if facility.due_date else "",
            "department_name": dept_name,
            "purpose": facility.purpose or "",
            "link": get_frontend_absolute_url("/wallet/credit-facility"),
        }
        CommunicationService.send_email(
            recipient=user,
            template="wallet_credit_facility_approved_email",
            template_context=ctx,
            created_by=facility.approved_by,
            metadata={
                "wallet_credit_facility_id": facility.pk,
                "public_reference": facility.public_reference,
            },
        )
        try:
            CommunicationService.send_push_notification(
                recipient=user,
                template="wallet_credit_facility_approved_push",
                template_context=ctx,
                created_by=facility.approved_by,
                metadata={"wallet_credit_facility_id": facility.pk},
            )
        except Exception:  # noqa: BLE001
            logger.debug("Push notification for credit approval skipped", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to notify user of wallet credit approval for %s",
            getattr(facility, "public_reference", facility.pk),
        )


@transaction.atomic
def create_and_submit_request(
    *,
    user,
    requested_amount,
    purpose: str,
    remarks: str = "",
    department_id: int | None = None,
) -> WalletCreditFacility:
    if not feature_enabled():
        raise WalletCreditError("FEATURE_DISABLED", "Wallet Credit Facility is not enabled.", status=403)
    assert_user_may_request_credit(user)
    if not department_id:
        raise WalletCreditError(
            "DEPARTMENT_REQUIRED",
            "Select the department for which you want to raise the credit request.",
        )
    purpose_clean = (purpose or "").strip()
    if not purpose_clean:
        raise WalletCreditError("PURPOSE_REQUIRED", "Purpose / reason is required.")
    policy = WalletCreditPolicy.get_solo()
    amount = money(requested_amount)
    if amount < money(policy.min_request_amount):
        raise WalletCreditError("AMOUNT_TOO_LOW", f"Minimum request amount is ₹{policy.min_request_amount}.")
    if amount > money(policy.max_credit_amount):
        raise WalletCreditError("AMOUNT_TOO_HIGH", f"Maximum request amount is ₹{policy.max_credit_amount}.")
    locked_user = type(user).objects.select_for_update().get(pk=user.pk)
    blocking = user_has_blocking_facility(locked_user)
    if blocking:
        raise WalletCreditError(
            "ACTIVE_CREDIT_EXISTS",
            f"You already have ₹{blocking.outstanding_amount or blocking.requested_amount} outstanding/active credit "
            f"({blocking.public_reference}). A new credit request can be submitted after the existing credit is fully settled.",
            status=409,
        )
    sub = resolve_subwallet_for_user(locked_user, int(department_id))
    facility = WalletCreditFacility.objects.create(
        public_reference=next_public_reference(),
        user=locked_user,
        department=sub.department,
        sub_wallet=sub,
        requested_amount=amount,
        purpose=purpose_clean,
        remarks="",
        status=WalletCreditFacilityStatus.SUBMITTED,
        submitted_at=timezone.now(),
        profile_snapshot=build_profile_snapshot(locked_user),
    )
    audit(
        facility,
        actor=locked_user,
        action="SUBMITTED",
        new=str(amount),
        reason=purpose_clean,
    )
    return facility


@transaction.atomic
def mark_under_review(facility: WalletCreditFacility, actor) -> WalletCreditFacility:
    facility = WalletCreditFacility.objects.select_for_update().get(pk=facility.pk)
    if facility.status not in {
        WalletCreditFacilityStatus.SUBMITTED,
        WalletCreditFacilityStatus.CLARIFICATION,
    }:
        raise WalletCreditError("INVALID_STATE", f"Cannot review facility in status {facility.status}.")
    prev = facility.status
    facility.status = WalletCreditFacilityStatus.UNDER_REVIEW
    facility.save(update_fields=["status", "updated_at"])
    audit(facility, actor=actor, action="UNDER_REVIEW", previous=prev, new=facility.status)
    return facility


@transaction.atomic
def approve_facility(
    *,
    facility: WalletCreditFacility,
    actor,
    approved_amount,
    due_date: date | None,
    reason: str = "",
) -> WalletCreditFacility:
    if getattr(actor, "user_type", None) != UserType.ADMIN:
        raise WalletCreditError("FORBIDDEN", "Only Main Administrator can approve credit.", status=403)
    facility = WalletCreditFacility.objects.select_for_update().get(pk=facility.pk)
    if facility.status not in {
        WalletCreditFacilityStatus.SUBMITTED,
        WalletCreditFacilityStatus.UNDER_REVIEW,
        WalletCreditFacilityStatus.CLARIFICATION,
    }:
        raise WalletCreditError("INVALID_STATE", f"Cannot approve facility in status {facility.status}.")
    approved = money(approved_amount)
    if approved <= 0:
        raise WalletCreditError("INVALID_AMOUNT", "Approved amount must be positive.")
    if approved > money(facility.requested_amount):
        raise WalletCreditError("INVALID_AMOUNT", "Approved amount cannot exceed requested amount.")
    policy = WalletCreditPolicy.get_solo()
    if approved > money(policy.max_credit_amount):
        raise WalletCreditError("AMOUNT_TOO_HIGH", f"Approved amount exceeds policy max ₹{policy.max_credit_amount}.")
    if approved != money(facility.requested_amount) and not (reason or "").strip():
        raise WalletCreditError("REASON_REQUIRED", "Reason is mandatory when reducing the approved amount.")
    if due_date is None:
        due_date = (timezone.localdate() + timedelta(days=int(policy.max_credit_duration_days)))
    prev_status = facility.status
    prev_amount = str(facility.approved_amount or "")
    facility.approved_amount = approved
    facility.approved_by = actor
    facility.approved_at = timezone.now()
    facility.approval_reason = (reason or "").strip()
    facility.due_date = due_date
    facility.status = WalletCreditFacilityStatus.APPROVED
    facility.profile_snapshot = {
        **(facility.profile_snapshot or {}),
        "at_approval": build_profile_snapshot(facility.user),
    }
    facility.save(
        update_fields=[
            "approved_amount",
            "approved_by",
            "approved_at",
            "approval_reason",
            "due_date",
            "status",
            "profile_snapshot",
            "updated_at",
        ]
    )
    audit(
        facility,
        actor=actor,
        action="APPROVED",
        previous=f"{prev_status}/{prev_amount}",
        new=f"{facility.status}/{approved}",
        reason=reason,
    )
    facility_id = facility.pk

    def _notify():
        try:
            fresh = WalletCreditFacility.objects.select_related(
                "user", "department", "approved_by"
            ).get(pk=facility_id)
            notify_user_credit_approved(fresh)
        except Exception:  # noqa: BLE001
            logger.exception("Credit approval notification failed for facility %s", facility_id)

    transaction.on_commit(_notify)
    return facility


@transaction.atomic
def reject_facility(*, facility: WalletCreditFacility, actor, reason: str) -> WalletCreditFacility:
    if getattr(actor, "user_type", None) != UserType.ADMIN:
        raise WalletCreditError("FORBIDDEN", "Only Main Administrator can reject credit.", status=403)
    if not (reason or "").strip():
        raise WalletCreditError("REASON_REQUIRED", "Rejection reason is mandatory.")
    facility = WalletCreditFacility.objects.select_for_update().get(pk=facility.pk)
    if facility.status in {
        WalletCreditFacilityStatus.CREDITED,
        WalletCreditFacilityStatus.PARTIALLY_SETTLED,
        WalletCreditFacilityStatus.CLEARED,
    }:
        raise WalletCreditError("INVALID_STATE", "Cannot reject after credit has been posted.")
    prev = facility.status
    facility.status = WalletCreditFacilityStatus.REJECTED
    facility.rejected_at = timezone.now()
    facility.rejected_by = actor
    facility.rejection_reason = reason.strip()
    facility.save(
        update_fields=["status", "rejected_at", "rejected_by", "rejection_reason", "updated_at"]
    )
    audit(facility, actor=actor, action="REJECTED", previous=prev, new=facility.status, reason=reason)
    return facility


@transaction.atomic
def return_for_clarification(*, facility: WalletCreditFacility, actor, reason: str) -> WalletCreditFacility:
    if getattr(actor, "user_type", None) != UserType.ADMIN:
        raise WalletCreditError("FORBIDDEN", "Only Main Administrator can return for clarification.", status=403)
    if not (reason or "").strip():
        raise WalletCreditError("REASON_REQUIRED", "Clarification reason is mandatory.")
    facility = WalletCreditFacility.objects.select_for_update().get(pk=facility.pk)
    prev = facility.status
    facility.status = WalletCreditFacilityStatus.CLARIFICATION
    facility.save(update_fields=["status", "updated_at"])
    audit(facility, actor=actor, action="CLARIFICATION", previous=prev, new=facility.status, reason=reason)
    return facility


@transaction.atomic
def post_credit(*, facility: WalletCreditFacility, actor) -> WalletCreditFacility:
    """Post approved credit to SubWallet ledger. Idempotent via credit_posting_key."""
    if getattr(actor, "user_type", None) not in {UserType.ADMIN, UserType.FINANCE}:
        raise WalletCreditError("FORBIDDEN", "Only Main Admin or Accounts can post credit.", status=403)
    facility = WalletCreditFacility.objects.select_for_update().get(pk=facility.pk)
    if facility.status == WalletCreditFacilityStatus.CREDITED and facility.credit_posting_key:
        return facility
    if facility.status != WalletCreditFacilityStatus.APPROVED:
        raise WalletCreditError("INVALID_STATE", "Facility must be APPROVED before credit posting.")
    if not facility.approved_amount or money(facility.approved_amount) <= 0:
        raise WalletCreditError("INVALID_AMOUNT", "Missing approved amount.")
    if not facility.sub_wallet_id:
        raise WalletCreditError("SUBWALLET_NOT_FOUND", "Facility has no linked sub-wallet.")
    posting_key = facility.credit_posting_key or f"credit-post-{facility.pk}"
    if WalletCreditLedgerEntry.objects.filter(reference=posting_key).exists():
        facility.status = WalletCreditFacilityStatus.CREDITED
        facility.credit_posting_key = posting_key
        facility.save(update_fields=["status", "credit_posting_key", "updated_at"])
        return facility
    sub = SubWallet.objects.select_for_update().get(pk=facility.sub_wallet_id)
    amount = money(facility.approved_amount)
    txn = sub.credit(
        amount,
        description=f"WALLET_CREDIT {facility.public_reference}",
        related_user=facility.user,
    )
    WalletCreditLedgerEntry.objects.create(
        facility=facility,
        kind=WalletCreditLedgerKind.WALLET_CREDIT,
        amount=amount,
        reference=posting_key,
        description=f"Credit posted for {facility.public_reference}",
        subwallet_transaction=txn,
        created_by=actor,
    )
    facility.credit_posting_key = posting_key
    facility.credited_at = timezone.now()
    facility.status = WalletCreditFacilityStatus.CREDITED
    recompute_outstanding(facility)
    facility.save(
        update_fields=[
            "credit_posting_key",
            "credited_at",
            "status",
            "outstanding_amount",
            "updated_at",
        ]
    )
    issue_invoice(facility=facility, actor=actor)
    audit(
        facility,
        actor=actor,
        action="CREDITED",
        new=str(amount),
        metadata={"subwallet_txn_id": txn.id},
    )
    return facility


@transaction.atomic
def issue_invoice(*, facility: WalletCreditFacility, actor) -> WalletCreditInvoice:
    existing = facility.invoices.exclude(status=WalletCreditInvoiceStatus.CANCELLED).order_by("-id").first()
    if existing:
        return existing
    inv = WalletCreditInvoice.objects.create(
        facility=facility,
        invoice_number=next_invoice_number(),
        status=WalletCreditInvoiceStatus.ISSUED,
        issue_date=timezone.localdate(),
        due_date=facility.due_date,
        approved_credit=money(facility.approved_amount or 0),
        amount_settled=Decimal("0.00"),
        outstanding_amount=money(facility.outstanding_amount),
        payment_instructions="Please settle outstanding credit via Wallet → Pay Outstanding Credit.",
        terms="Credit facility settlement as per IIC Equipment Booking Portal policy.",
        issued_by=actor,
    )
    audit(facility, actor=actor, action="INVOICE_ISSUED", new=inv.invoice_number)
    return inv


@transaction.atomic
def repay_from_wallet(
    *,
    facility: WalletCreditFacility,
    actor,
    amount,
    mode: str = "wallet_debit",
    utr_or_reference: str = "",
    remarks: str = "",
) -> WalletCreditPayment:
    facility = WalletCreditFacility.objects.select_for_update().get(pk=facility.pk)
    if facility.user_id != actor.id and getattr(actor, "user_type", None) not in {
        UserType.ADMIN,
        UserType.FINANCE,
    }:
        raise WalletCreditError("FORBIDDEN", "Not allowed to repay this facility.", status=403)
    if facility.status not in {
        WalletCreditFacilityStatus.CREDITED,
        WalletCreditFacilityStatus.PARTIALLY_SETTLED,
    }:
        raise WalletCreditError("INVALID_STATE", f"Cannot repay facility in status {facility.status}.")
    pay = money(amount)
    outstanding = recompute_outstanding(facility)
    if pay <= 0:
        raise WalletCreditError("INVALID_AMOUNT", "Repayment amount must be positive.")
    if pay > outstanding:
        raise WalletCreditError("INVALID_AMOUNT", f"Repayment exceeds outstanding ₹{outstanding}.")
    if not facility.sub_wallet_id:
        raise WalletCreditError("SUBWALLET_NOT_FOUND", "Facility has no linked sub-wallet.")
    sub = SubWallet.objects.select_for_update().get(pk=facility.sub_wallet_id)
    # Repayment debits wallet and reduces outstanding; floor is 0 (no overdraft).
    txn = sub.debit(
        pay,
        description=f"CREDIT_REPAYMENT {facility.public_reference}",
        related_user=facility.user,
        minimum_balance_after=Decimal("0.00"),
    )
    ledger = WalletCreditLedgerEntry.objects.create(
        facility=facility,
        kind=WalletCreditLedgerKind.CREDIT_REPAYMENT,
        amount=pay,
        reference=next_ledger_reference("REPAY"),
        description=f"Repayment for {facility.public_reference}",
        subwallet_transaction=txn,
        created_by=actor,
    )
    outstanding = recompute_outstanding(facility)
    if outstanding == 0:
        facility.status = WalletCreditFacilityStatus.CLEARED
        facility.cleared_at = timezone.now()
    else:
        facility.status = WalletCreditFacilityStatus.PARTIALLY_SETTLED
    facility.save(update_fields=["outstanding_amount", "status", "cleared_at", "updated_at"])
    invoice = facility.invoices.exclude(status=WalletCreditInvoiceStatus.CANCELLED).order_by("-id").first()
    if invoice:
        settled = money(
            facility.ledger_entries.filter(kind=WalletCreditLedgerKind.CREDIT_REPAYMENT).aggregate(s=Sum("amount"))["s"]
        )
        invoice.amount_settled = settled
        invoice.outstanding_amount = outstanding
        if outstanding == 0:
            invoice.status = WalletCreditInvoiceStatus.PAID
        else:
            invoice.status = WalletCreditInvoiceStatus.PARTIALLY_PAID
        invoice.save(update_fields=["amount_settled", "outstanding_amount", "status", "updated_at"])
    payment = WalletCreditPayment.objects.create(
        facility=facility,
        invoice=invoice,
        ledger_entry=ledger,
        amount=pay,
        payment_date=timezone.localdate(),
        mode=mode or "wallet_debit",
        utr_or_reference=utr_or_reference or "",
        remarks=remarks or "",
        receipt_number=next_receipt_number(),
        recorded_by=actor,
    )
    audit(
        facility,
        actor=actor,
        action="REPAYMENT",
        new=str(pay),
        metadata={"receipt": payment.receipt_number, "outstanding": str(outstanding)},
    )
    return payment


def mark_overdue_facilities() -> int:
    today = timezone.localdate()
    qs = WalletCreditFacility.objects.filter(
        status__in={
            WalletCreditFacilityStatus.CREDITED,
            WalletCreditFacilityStatus.PARTIALLY_SETTLED,
        },
        due_date__lt=today,
        outstanding_amount__gt=0,
    )
    count = 0
    for facility in qs:
        # Status stays CREDITED/PARTIALLY_SETTLED; overdue is derived for invoices/notifications.
        inv = facility.invoices.filter(status=WalletCreditInvoiceStatus.ISSUED).first()
        if inv:
            inv.status = WalletCreditInvoiceStatus.OVERDUE
            inv.save(update_fields=["status", "updated_at"])
            count += 1
            audit(facility, actor=None, action="OVERDUE_MARKED", new=str(facility.outstanding_amount))
    return count


def reconcile_facility(facility: WalletCreditFacility) -> dict:
    outstanding = recompute_outstanding(facility)
    credited = money(
        facility.ledger_entries.filter(kind=WalletCreditLedgerKind.WALLET_CREDIT).aggregate(s=Sum("amount"))["s"]
    )
    repaid = money(
        facility.ledger_entries.filter(kind=WalletCreditLedgerKind.CREDIT_REPAYMENT).aggregate(s=Sum("amount"))["s"]
    )
    invoice = facility.invoices.exclude(status=WalletCreditInvoiceStatus.CANCELLED).order_by("-id").first()
    paid = money(facility.payments.aggregate(s=Sum("amount"))["s"])
    ok = (
        outstanding == money(facility.outstanding_amount)
        and repaid == paid
        and (invoice is None or money(invoice.outstanding_amount) == outstanding)
    )
    return {
        "public_reference": facility.public_reference,
        "status": facility.status,
        "approved": str(money(facility.approved_amount or 0)),
        "credited": str(credited),
        "repaid": str(repaid),
        "outstanding": str(outstanding),
        "invoice_outstanding": str(money(invoice.outstanding_amount) if invoice else 0),
        "payments_total": str(paid),
        "consistent": ok,
    }
