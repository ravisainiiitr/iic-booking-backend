"""
Import IIC wallet recharge text file: parse rows, match user by emp_id, credit sub-wallet, record to avoid double-import.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import (
    Department,
    DepartmentType,
    User,
    Wallet,
    WalletRechargeImportRecord,
    WalletRechargeParseEntry,
    WalletRechargeCreditFacilityStatus,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
    SubWallet,
)
from .wallet_recharge_parser import (
    financial_year_start_for_date,
    parse_wallet_recharge_file,
)

logger = logging.getLogger(__name__)


def _resolve_department(
    user: User,
    dept_hint: Optional[str],
    default_department_id: Optional[int],
) -> Optional[Department]:
    """
    Resolve department when there is no matching pending WalletRechargeRequest.

    Order: operator default (import UI), parse/receipt dept hint, then user's internal HR department.
    """
    if default_department_id:
        try:
            return Department.objects.get(pk=default_department_id, department_type=DepartmentType.INTERNAL)
        except Department.DoesNotExist:
            pass
    if dept_hint:
        hint = dept_hint.upper().strip()
        if hint:
            qs = Department.objects.filter(department_type=DepartmentType.INTERNAL)
            for d in qs:
                if hint in (d.name or "").upper() or (d.code and hint in d.code.upper()):
                    return d
    if user.department_id and getattr(user.department, "department_type", None) == DepartmentType.INTERNAL:
        return user.department
    return None


def _first_pending_recharge_request_for_import(user: User, amount: Decimal) -> Optional[WalletRechargeRequest]:
    """
    Oldest PENDING + OTP-verified request for this user and amount (same rule as parse-entry matcher).
    Department on the request is what the user selected when raising the recharge.
    """
    return (
        WalletRechargeRequest.objects.filter(
            status=WalletRechargeRequestStatus.PENDING,
            user=user,
            amount=amount,
            user_otp_verified=True,
            department_id__isnull=False,
        )
        .select_related("department")
        .order_by("created_at")
        .first()
    )


def _write_recharge_request_approved_from_import(
    req: WalletRechargeRequest,
    *,
    receipt_no: str,
    via_parse_entry_matcher: bool,
) -> None:
    """Persist APPROVED state after import credit (no notifications; may run inside atomic)."""
    msg = (
        f"Auto-completed: receipt matched IIC account import (Receipt {receipt_no})."
        if via_parse_entry_matcher
        else f"Auto-completed: wallet import (Receipt {receipt_no})."
    )
    req.status = WalletRechargeRequestStatus.APPROVED
    req.responded_at = timezone.now()
    req.response_message = msg
    req.approved_by_email = getattr(
        settings, "ACCOUNTS_EMAIL", "accounts@iicbooking.iitr.ac.in"
    )
    req.credit_facility_status = WalletRechargeCreditFacilityStatus.INACTIVE
    req.credit_facility_opted_in = False
    req.save(
        update_fields=[
            "status",
            "responded_at",
            "response_message",
            "approved_by_email",
            "credit_facility_status",
            "credit_facility_opted_in",
        ]
    )


def _send_recharge_request_approved_notifications_safe(req: WalletRechargeRequest) -> None:
    try:
        from iic_booking.communication.wallet_notifications import (
            send_wallet_recharge_request_notifications,
        )
        from iic_booking.communication.styled_transactional_emails import (
            send_wallet_recharge_approved_faculty_email,
        )

        req.refresh_from_db()
        send_wallet_recharge_request_notifications(req, "APPROVED")
        try:
            send_wallet_recharge_approved_faculty_email(req)
        except Exception:
            pass
    except Exception as ex:
        logger.warning("Recharge approve-after-import notify failed for request %s: %s", req.pk, ex)


def import_wallet_recharge_rows(
    rows: List[Dict[str, Any]],
    default_department_id: Optional[int] = None,
    dry_run: bool = False,
    *,
    credit_department_id: Optional[int] = None,
) -> Tuple[int, int, List[str], List[str]]:
    """
    Process parsed wallet recharge rows: find user by emp_no, credit sub-wallet, create import record.

    Args:
        rows: From parse_wallet_recharge_file().
        default_department_id: Optional internal department pk when no pending recharge request matches the row.
        dry_run: If True, do not credit or create records; only validate and return would-be stats.
        credit_department_id: When set (e.g. parse-entry matcher), force this internal department. Caller approves
            the request afterward. When unset, a PENDING OTP-verified request (same user + amount) wins over
            parse dept text and import defaults.

    Returns:
        (credited_count, skipped_count, list of error/warning messages, list of receipt_nos credited).
    """
    credited = 0
    skipped = 0
    errors: List[str] = []
    processed_receipts: List[str] = []

    for row in rows:
        receipt_no = (row.get("receipt_no") or "").strip()
        if not receipt_no:
            errors.append("Row missing receipt_no; skipped.")
            skipped += 1
            continue
        amount = row.get("amount")
        if not amount or not isinstance(amount, Decimal) or amount <= 0:
            errors.append(f"Receipt {receipt_no}: invalid amount; skipped.")
            skipped += 1
            continue
        emp_no = row.get("emp_no")
        if not emp_no:
            errors.append(f"Receipt {receipt_no}: no EMP NO in 'Received From'; skipped.")
            skipped += 1
            continue
        try:
            user = User.objects.get(emp_id=emp_no)
        except User.DoesNotExist:
            errors.append(f"Receipt {receipt_no}: no user with emp_id={emp_no}; skipped.")
            skipped += 1
            continue
        if not user.can_have_wallet():
            errors.append(f"Receipt {receipt_no}: user {user.email} cannot have wallet; skipped.")
            skipped += 1
            continue
        wallet, _ = Wallet.objects.get_or_create(user=user, defaults={})

        dated = row.get("dated")
        if dated:
            if isinstance(dated, date):
                fy_start = date(dated.year, 4, 1) if dated.month >= 4 else date(dated.year - 1, 4, 1)
            else:
                fy_start = financial_year_start_for_date(dated)
        else:
            today = timezone.localdate()
            fy_start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)

        # Duplicate check: same (date, receipt_no, emp_no) must not be credited again
        dup_qs = WalletRechargeImportRecord.objects.filter(receipt_no=receipt_no, user=user)
        if dated is not None:
            dup_qs = dup_qs.filter(dated=dated)
        if dup_qs.exists():
            skipped += 1
            continue

        pending_req_to_finalize: Optional[WalletRechargeRequest] = None

        if credit_department_id is not None:
            try:
                department = Department.objects.get(
                    pk=credit_department_id, department_type=DepartmentType.INTERNAL
                )
            except Department.DoesNotExist:
                errors.append(
                    f"Receipt {receipt_no}: credit_department_id {credit_department_id} is not a valid internal department; skipped."
                )
                skipped += 1
                continue
        else:
            pending_for_dept = _first_pending_recharge_request_for_import(user, amount)
            if pending_for_dept and pending_for_dept.department_id:
                department = pending_for_dept.department
                pending_req_to_finalize = pending_for_dept
            else:
                department = _resolve_department(
                    user,
                    row.get("dept_hint"),
                    default_department_id,
                )
        if not department:
            errors.append(f"Receipt {receipt_no}: could not resolve department for user {user.email}; skipped.")
            skipped += 1
            continue

        if dry_run:
            credited += 1
            continue

        recharge_pk_to_notify: Optional[int] = None
        with transaction.atomic():
            sub_wallet, _ = SubWallet.objects.get_or_create(
                wallet=wallet,
                department=department,
                defaults={"balance": Decimal("0.00")},
            )
            description = f"IIC wallet recharge – Receipt No. {receipt_no}"
            if row.get("payment_details"):
                description += f" – {row['payment_details'][:100]}"
            if row.get("name"):
                description += f" – {row['name'][:80]}"
            transaction_obj = sub_wallet.credit(amount, description, related_user=user)
            WalletRechargeImportRecord.objects.create(
                receipt_no=receipt_no,
                financial_year_start=fy_start,
                user=user,
                department=department,
                amount=amount,
                dated=dated,
                received_from_raw=row.get("received_from") or "",
                remarks=row.get("remarks") or "",
            )
            if pending_req_to_finalize is not None:
                locked_req = (
                    WalletRechargeRequest.objects.select_for_update()
                    .filter(
                        pk=pending_req_to_finalize.pk,
                        status=WalletRechargeRequestStatus.PENDING,
                    )
                    .first()
                )
                if locked_req:
                    _write_recharge_request_approved_from_import(
                        locked_req,
                        receipt_no=receipt_no,
                        via_parse_entry_matcher=False,
                    )
                    recharge_pk_to_notify = locked_req.pk
            credited += 1
            logger.info("Credited Receipt %s FY %s → %s ₹%s", receipt_no, fy_start, user.email, amount)
            processed_receipts.append(receipt_no)
            # Send email to wallet owner (CC office inbox for import credits)
            try:
                from iic_booking.communication.wallet_notifications import send_sub_wallet_transaction_notifications

                cc_raw = getattr(settings, "WALLET_IMPORT_CREDIT_CC", "iicbooking@iitr.ac.in")
                if isinstance(cc_raw, str):
                    cc_list = [x.strip() for x in cc_raw.split(",") if x.strip()]
                else:
                    cc_list = [str(x).strip() for x in (cc_raw or []) if str(x).strip()]
                send_sub_wallet_transaction_notifications(
                    transaction_obj, booking=None, booking_user=None, cc_emails=cc_list or None
                )
            except Exception as e:
                logger.warning("Failed to send wallet credit email to %s: %s", user.email, e)

        if recharge_pk_to_notify is not None:
            try:
                _send_recharge_request_approved_notifications_safe(
                    WalletRechargeRequest.objects.get(pk=recharge_pk_to_notify)
                )
            except WalletRechargeRequest.DoesNotExist:
                pass

    return credited, skipped, errors, processed_receipts


def _parse_entry_to_import_row(entry: WalletRechargeParseEntry) -> Optional[Dict[str, Any]]:
    """Build import row dict from stored parse entry (same shape as parser output)."""
    try:
        amount = Decimal(str((entry.amount or "").replace(",", "").strip()))
    except Exception:
        return None
    if amount <= 0:
        return None
    emp_no = (entry.emp_no or "").strip()
    receipt_no = (entry.receipt_no or "").strip()
    if not emp_no or not receipt_no:
        return None
    name = (entry.name or "").strip()
    received_from = name
    if emp_no:
        received_from = f"{received_from} EMP NO-{emp_no}".strip()
    if entry.department:
        received_from = f"{received_from} DEPT-OF {entry.department}".strip()
    return {
        "receipt_no": receipt_no,
        "amount": amount,
        "emp_no": emp_no,
        "dated": entry.dated,
        "received_from": received_from,
        "name": name,
        "payment_details": (entry.payment or "")[:5000],
        "dept_hint": (entry.department or "").strip(),
        "remarks": "",
    }


def _import_record_exists_for_parse_entry(entry: WalletRechargeParseEntry) -> bool:
    emp_no = (entry.emp_no or "").strip()
    receipt_no = (entry.receipt_no or "").strip()
    if not emp_no or not receipt_no:
        return False
    qs = WalletRechargeImportRecord.objects.filter(receipt_no=receipt_no, user__emp_id=emp_no)
    if entry.dated is not None:
        qs = qs.filter(dated=entry.dated)
    return qs.exists()


def match_pending_recharge_requests_to_parse_entries() -> Tuple[int, List[str]]:
    """
    For each parse row not yet imported, find a PENDING recharge request (OTP verified) for the same
    user (emp_id) and amount; credit via import and mark the request approved.
    """
    matched = 0
    errors: List[str] = []
    for entry in WalletRechargeParseEntry.objects.all().order_by("-created_at"):
        if _import_record_exists_for_parse_entry(entry):
            continue
        row = _parse_entry_to_import_row(entry)
        if not row:
            continue
        emp_key = (row.get("emp_no") or "").strip()
        user = User.objects.filter(emp_id=emp_key).first()
        if user is None and emp_key:
            user = User.objects.filter(emp_id__iexact=emp_key).first()
        if user is None:
            continue
        reqs = (
            WalletRechargeRequest.objects.filter(
                status=WalletRechargeRequestStatus.PENDING,
                user=user,
                amount=row["amount"],
                user_otp_verified=True,
            )
            .select_related("department")
            .order_by("created_at")
        )
        req = reqs.first()
        if not req:
            continue
        dept_id = req.department_id
        credited, _skipped, err_list, _receipts = import_wallet_recharge_rows(
            [row],
            default_department_id=dept_id,
            dry_run=False,
            credit_department_id=dept_id,
        )
        errors.extend(err_list)
        if credited > 0:
            locked_req = None
            with transaction.atomic():
                locked_req = (
                    WalletRechargeRequest.objects.select_for_update()
                    .filter(pk=req.pk, status=WalletRechargeRequestStatus.PENDING)
                    .first()
                )
                if locked_req:
                    _write_recharge_request_approved_from_import(
                        locked_req,
                        receipt_no=str(entry.receipt_no or ""),
                        via_parse_entry_matcher=True,
                    )
            if locked_req:
                _send_recharge_request_approved_notifications_safe(locked_req)
            matched += 1
    return matched, errors
