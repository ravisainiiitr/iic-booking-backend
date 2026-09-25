"""
Import IIC wallet recharge text file: parse rows, match user by emp_id, credit sub-wallet, record to avoid double-import.
"""

import logging
from datetime import date
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import (
    Department,
    DepartmentType,
    User,
    Wallet,
    WalletRechargeImportRecord,
    WalletRechargeParseEntry,
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

        used_by = receipt_used_by_request(receipt_no, dated)
        if used_by is not None:
            errors.append(
                f"Receipt {receipt_no}: already used for recharge request {used_by.request_id_display}; skipped."
            )
            skipped += 1
            continue

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
            # A direct credit here plus a later approval of the pending request would credit twice,
            # so the receipt must be consumed through the request instead.
            open_req = _first_pending_recharge_request_for_import(user, amount) or (
                WalletRechargeRequest.objects.filter(
                    status=WalletRechargeRequestStatus.APPROVED,
                    user=user,
                    amount=amount,
                    cashbook_receipt_no="",
                )
                .order_by("created_at")
                .first()
            )
            if open_req is not None:
                errors.append(
                    f"Receipt {receipt_no}: matches {open_req.get_status_display().lower()} recharge request "
                    f"{open_req.request_id_display}; match it from Wallet Recharge Requests instead. Not credited."
                )
                skipped += 1
                continue
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
            # Intentionally do not auto-approve pending WalletRechargeRequest rows.
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


def _normalize_grant_code(value: Optional[str]) -> str:
    """Normalize grant / project codes for comparison (e.g. IIC-000-002)."""
    raw = (value or "").strip().upper()
    return "".join(ch for ch in raw if ch.isalnum() or ch in "-_")


def _parse_entry_grant_code(entry: WalletRechargeParseEntry) -> str:
    return _normalize_grant_code(getattr(entry, "credited_to_project_no", "") or "")


def _request_matches_grant(req: WalletRechargeRequest, grant: str) -> bool:
    if not grant:
        return False
    for candidate in (
        req.department_grant_code,
        req.project_grant_code,
        getattr(req.department, "internal_grant_code", None) if getattr(req, "department", None) else None,
    ):
        if _normalize_grant_code(candidate or "") == grant:
            return True
    return False


class CashbookMatchError(Exception):
    """A cash-book entry cannot be applied to a recharge request (mismatch or already consumed)."""


def _parse_entry_amount(entry: WalletRechargeParseEntry) -> Optional[Decimal]:
    try:
        amount = Decimal(str((entry.amount or "").replace(",", "").strip()))
    except Exception:
        return None
    return amount if amount > 0 else None


def _request_emp_no(req: WalletRechargeRequest) -> str:
    emp = (req.employee_number or "").strip()
    if not emp and req.user_id:
        emp = (getattr(req.user, "emp_id", "") or "").strip()
    return emp.upper()


def receipt_used_by_request(
    receipt_no: str, dated: Optional[date], *, exclude_request_id: Optional[int] = None
) -> Optional[WalletRechargeRequest]:
    """Request that already consumed this cash-book receipt (a missing date on either side is treated as the same receipt)."""
    receipt_no = (receipt_no or "").strip()
    if not receipt_no:
        return None
    qs = WalletRechargeRequest.objects.filter(cashbook_receipt_no=receipt_no)
    if dated is not None:
        qs = qs.filter(Q(cashbook_receipt_date=dated) | Q(cashbook_receipt_date__isnull=True))
    if exclude_request_id:
        qs = qs.exclude(pk=exclude_request_id)
    return qs.order_by("pk").first()


def parse_entry_consumed_reason(entry: WalletRechargeParseEntry) -> str:
    """Why this entry can no longer be applied to a request ('' when it is still available)."""
    linked = getattr(entry, "matched_recharge_request", None) if entry.pk else None
    if linked is not None:
        return f"Already linked to {linked.request_id_display}."
    used_by = receipt_used_by_request(entry.receipt_no, entry.dated)
    if used_by is not None:
        return f"Receipt {entry.receipt_no} already used for {used_by.request_id_display}."
    if _import_record_exists_for_parse_entry(entry):
        return f"Receipt {entry.receipt_no} was already credited via cash-book import."
    return ""


def serialize_parse_entry_brief(entry: WalletRechargeParseEntry, *, emp_match: Optional[bool] = None) -> Dict[str, Any]:
    out = {
        "id": entry.id,
        "receipt_no": (entry.receipt_no or "").strip(),
        "date": entry.dated.isoformat() if entry.dated else None,
        "amount": (entry.amount or "").strip(),
        "emp_no": (entry.emp_no or "").strip(),
        "name": (entry.name or "").strip(),
        "department": (entry.department or "").strip(),
        "credited_to_project_no": (entry.credited_to_project_no or "").strip(),
        "payment": (entry.payment or "")[:300],
    }
    if emp_match is not None:
        out["emp_match"] = emp_match
    return out


class CashbookIndex:
    """
    In-memory view of available (unconsumed) cash-book entries, built once per list request
    so each recharge row can be matched without extra queries.
    """

    def __init__(self) -> None:
        used_receipts: Dict[str, List[Optional[date]]] = {}
        for rno, rdate in WalletRechargeRequest.objects.exclude(cashbook_receipt_no="").values_list(
            "cashbook_receipt_no", "cashbook_receipt_date"
        ):
            used_receipts.setdefault(rno.strip(), []).append(rdate)
        imported = set(
            (rno.strip(), rdate, (emp or "").strip().upper())
            for rno, rdate, emp in WalletRechargeImportRecord.objects.values_list(
                "receipt_no", "dated", "user__emp_id"
            )
        )
        imported_no_date = set((rno, emp) for rno, _d, emp in imported)

        self.by_amount: Dict[Decimal, List[Dict[str, Any]]] = {}
        entries = WalletRechargeParseEntry.objects.select_related("matched_recharge_request").order_by("-dated", "-id")
        for entry in entries:
            amount = _parse_entry_amount(entry)
            if amount is None:
                continue
            receipt_no = (entry.receipt_no or "").strip()
            emp = (entry.emp_no or "").strip().upper()
            if getattr(entry, "matched_recharge_request", None) is not None:
                continue
            dates = used_receipts.get(receipt_no)
            if dates is not None and (entry.dated is None or None in dates or entry.dated in dates):
                continue
            if entry.dated is not None:
                if (receipt_no, entry.dated, emp) in imported:
                    continue
            elif (receipt_no, emp) in imported_no_date:
                continue
            self.by_amount.setdefault(amount, []).append(
                {"entry": entry, "grant": _parse_entry_grant_code(entry), "emp": emp}
            )

    def candidates_for(self, req: WalletRechargeRequest) -> List[Dict[str, Any]]:
        if req.cashbook_receipt_no or req.status not in (
            WalletRechargeRequestStatus.PENDING,
            WalletRechargeRequestStatus.APPROVED,
        ):
            return []
        req_emp = _request_emp_no(req)
        out = []
        for info in self.by_amount.get(Decimal(req.amount), []):
            emp_match = bool(info["emp"]) and info["emp"] == req_emp
            if info["grant"]:
                if not _request_matches_grant(req, info["grant"]):
                    continue
            elif not emp_match:
                continue
            out.append({**info, "emp_match": emp_match})
        out.sort(key=lambda c: not c["emp_match"])
        return out


def _entry_matches_request(entry: WalletRechargeParseEntry, req: WalletRechargeRequest) -> Tuple[bool, str]:
    amount = _parse_entry_amount(entry)
    if amount is None or amount != Decimal(req.amount):
        return False, f"Amount mismatch: cash-book ₹{entry.amount} vs request ₹{req.amount}."
    grant = _parse_entry_grant_code(entry)
    entry_emp = (entry.emp_no or "").strip().upper()
    if grant:
        if not _request_matches_grant(req, grant):
            return False, (
                f"Credited to Project No. {entry.credited_to_project_no} does not match the request grant "
                f"({req.department_grant_code or req.project_grant_code or '—'})."
            )
    elif not entry_emp or entry_emp != _request_emp_no(req):
        return False, "Cash-book row has no Project No. and its Emp No. does not match the requester."
    return True, ""


def link_cashbook_entry_to_request(
    request_id: int,
    entry_id: int,
    *,
    actor=None,
    actor_email: str = "",
) -> Tuple[WalletRechargeRequest, str]:
    """
    Apply one cash-book entry to one recharge request, atomically and at most once.

    PENDING  -> approve (single wallet credit via approve_request), record receipt, verify fund receipt.
    APPROVED -> record receipt and verify fund receipt (no credit).

    Returns (request, outcome) where outcome is "approved" or "verified".
    Raises CashbookMatchError when the pair is invalid or either side was already consumed.
    """
    from django.db import IntegrityError

    from .wallet_recharge_workflow import (
        RechargeAlreadyProcessed,
        append_audit_log,
        approve_request,
        notify_stakeholders_of_decision,
    )

    email = (actor_email or getattr(actor, "email", "") or "sric-cashbook-auto").strip()
    try:
        with transaction.atomic():
            try:
                entry = WalletRechargeParseEntry.objects.select_for_update().get(pk=entry_id)
            except WalletRechargeParseEntry.DoesNotExist:
                raise CashbookMatchError("Cash-book entry not found (it may have been cleared).")
            locked = (
                WalletRechargeRequest.objects.select_for_update(of=("self",))
                .select_related("user", "department")
                .get(pk=request_id)
            )
            if locked.cashbook_receipt_no or locked.cashbook_parse_entry_id:
                raise CashbookMatchError(
                    f"{locked.request_id_display} is already matched to cash-book receipt "
                    f"{locked.cashbook_receipt_no or '—'}."
                )
            if locked.status not in (WalletRechargeRequestStatus.PENDING, WalletRechargeRequestStatus.APPROVED):
                raise CashbookMatchError(
                    f"{locked.request_id_display} is {locked.get_status_display()}; only pending or approved "
                    "requests can be matched."
                )
            reason = parse_entry_consumed_reason(entry)
            if reason:
                raise CashbookMatchError(reason)
            ok, why = _entry_matches_request(entry, locked)
            if not ok:
                raise CashbookMatchError(why)

            receipt_no = (entry.receipt_no or "").strip()
            outcome = "verified"
            if locked.status == WalletRechargeRequestStatus.PENDING:
                try:
                    locked = approve_request(
                        locked,
                        response_message=f"Approved against SRIC cash-book receipt {receipt_no}.",
                        actor=actor,
                        actor_email=email,
                    )
                except RechargeAlreadyProcessed as exc:
                    raise CashbookMatchError(f"{locked.request_id_display} was already processed ({exc}).")
                except ValueError as exc:
                    raise CashbookMatchError(str(exc))
                outcome = "approved"
                dated = entry.dated
                if dated:
                    fy_start = date(dated.year, 4, 1) if dated.month >= 4 else date(dated.year - 1, 4, 1)
                else:
                    today = timezone.localdate()
                    fy_start = date(today.year, 4, 1) if today.month >= 4 else date(today.year - 1, 4, 1)
                WalletRechargeImportRecord.objects.create(
                    receipt_no=receipt_no,
                    financial_year_start=fy_start,
                    user=locked.user,
                    department=locked.department,
                    amount=locked.amount,
                    dated=entry.dated,
                    received_from_raw=f"{entry.name or ''} EMP NO-{entry.emp_no or ''}".strip(),
                    remarks=f"Credited via {locked.request_id_display} approval (cash-book match).",
                )

            now = timezone.now()
            locked.cashbook_parse_entry = entry
            locked.cashbook_receipt_no = receipt_no
            locked.cashbook_receipt_date = entry.dated
            locked.cashbook_matched_at = now
            fields = ["cashbook_parse_entry", "cashbook_receipt_no", "cashbook_receipt_date", "cashbook_matched_at", "updated_at"]
            if not locked.fund_receipt_verified:
                locked.fund_receipt_verified = True
                locked.fund_receipt_verified_by = actor if getattr(actor, "pk", None) else None
                locked.fund_receipt_verified_at = now
                locked.fund_receipt_verification_remarks = (
                    f"Matched SRIC cash-book receipt {receipt_no}"
                    f"{' dated ' + entry.dated.isoformat() if entry.dated else ''}"
                    f" (Credited to Project No. {entry.credited_to_project_no or '—'})."
                )
                fields += [
                    "fund_receipt_verified",
                    "fund_receipt_verified_by",
                    "fund_receipt_verified_at",
                    "fund_receipt_verification_remarks",
                ]
            locked.save(update_fields=fields)
            append_audit_log(
                locked,
                action="cashbook_matched",
                from_status=locked.status,
                to_status=locked.status,
                actor=actor,
                actor_email=email,
                message=f"Cash-book receipt {receipt_no} matched ({outcome}).",
                metadata={
                    "parse_entry_id": entry.id,
                    "receipt_no": receipt_no,
                    "dated": entry.dated.isoformat() if entry.dated else None,
                    "credited_to_project_no": entry.credited_to_project_no or "",
                    "outcome": outcome,
                },
            )
    except IntegrityError:
        raise CashbookMatchError("This cash-book receipt was already used for another recharge request.")

    if outcome == "approved":
        approved_req = locked
        transaction.on_commit(lambda: notify_stakeholders_of_decision(approved_req))
    return locked, outcome


def match_pending_recharge_requests_to_parse_entries() -> Tuple[int, List[str]]:
    """
    Auto-apply cash-book entries where the pairing is unambiguous in both directions
    (one available entry for the request, and no other eligible request competing for that entry).

    Match rule: amount equal AND Credited to Project No. == request grant code; rows without a
    Project No. need an exact Emp No. match. Auto-apply additionally requires the Emp No. to match
    whenever the cash-book row has one. Everything else is left for manual matching on the
    Wallet Recharge Requests page.
    """
    matched = 0
    errors: List[str] = []
    index = CashbookIndex()
    eligible = list(
        WalletRechargeRequest.objects.filter(cashbook_receipt_no="")
        .filter(
            Q(
                status=WalletRechargeRequestStatus.PENDING,
                user_otp_verified=True,
                department_id__isnull=False,
            )
            | Q(status=WalletRechargeRequestStatus.APPROVED, fund_receipt_verified=False)
        )
        .select_related("user", "department")
        .order_by("created_at")
    )

    # The credit grant (e.g. IIC-000-002) is shared by all requests, so auto-apply only when the
    # cash-book Emp No. identifies the requester (or the row has no Emp No. at all).
    def _auto_ok(c: Dict[str, Any]) -> bool:
        return c["emp_match"] or not c["emp"]

    req_cands: Dict[int, List[Dict[str, Any]]] = {}
    entry_reqs: Dict[int, List[WalletRechargeRequest]] = {}
    for req in eligible:
        cands = [c for c in index.candidates_for(req) if _auto_ok(c)]
        req_cands[req.id] = cands
        for c in cands:
            entry_reqs.setdefault(c["entry"].id, []).append(req)

    used_entries: set = set()
    for req in eligible:
        cands = req_cands.get(req.id) or []
        if len(cands) != 1:
            continue
        entry = cands[0]["entry"]
        if entry.id in used_entries:
            continue
        pool = entry_reqs.get(entry.id) or []
        if len(pool) != 1 or pool[0].id != req.id:
            continue
        try:
            link_cashbook_entry_to_request(req.id, entry.id, actor_email="sric-cashbook-auto")
            used_entries.add(entry.id)
            matched += 1
        except CashbookMatchError as exc:
            errors.append(f"Receipt {entry.receipt_no} → {req.request_id_display}: {exc}")
        except Exception as exc:
            logger.exception("Cash-book auto-match failed for %s / entry %s", req.pk, entry.pk)
            errors.append(f"Receipt {entry.receipt_no} → {req.request_id_display}: {exc}")
    return matched, errors


def store_parsed_cashbook_rows(rows: List[Dict[str, Any]], *, source_imap_uid: Optional[str] = None) -> int:
    """Upsert parser output into WalletRechargeParseEntry (key: receipt_no, date, emp_no). Returns rows stored."""
    stored = 0
    for row in rows:
        receipt_no = (row.get("receipt_no") or "").strip()[:50]
        emp_no = (row.get("emp_no") or "").strip()[:50]
        amount = row.get("amount")
        if not receipt_no or not emp_no or amount is None:
            continue
        defaults = {
            "name": (row.get("name") or "").strip()[:255],
            "department": (row.get("dept_hint") or "").strip()[:255],
            "amount": f"{amount:,.2f}"[:50],
            "payment": (row.get("payment_details") or "")[:5000],
            "credited_to_project_no": (row.get("credited_to_project_no") or "").strip()[:100],
        }
        if source_imap_uid:
            defaults["source_imap_uid"] = source_imap_uid[:32]
        WalletRechargeParseEntry.objects.update_or_create(
            receipt_no=receipt_no,
            dated=row.get("dated"),
            emp_no=emp_no,
            defaults=defaults,
        )
        stored += 1
    return stored
