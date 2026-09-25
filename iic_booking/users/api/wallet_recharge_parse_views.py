"""Wallet recharge cash-book TXT parse + IMAP fetch APIs."""

import logging
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes, parser_classes
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models import (
    Department,
    DepartmentType,
    User,
    UserType,
    Wallet,
    WalletRechargeParseEntry,
    WalletRechargeImportRecord,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
    WalletRechargeCreditFacilityStatus,
    SubWallet,
    Project,
)
from iic_booking.users import imap_fetch
from iic_booking.users import wallet_recharge_parser
from iic_booking.users import wallet_recharge_import
from iic_booking.users.repositories.wallet_repository import (
    WalletRepository,
    resolve_internal_department_for_wallet_recharge,
)
from iic_booking.users.serializers.wallet_serializer import WalletRechargeRequestSerializer
from iic_booking.users.api.wallet_views import (
    _notify_accounts_team_and_faculty_after_recharge_request_user_verified,
)

logger = logging.getLogger(__name__)


def _is_wallet_recharge_ops_staff(user) -> bool:
    """Admin or Accounts In Charge (finance): wallet recharge parse, IMAP, manual credit."""
    ut = getattr(user, "user_type", None)
    return ut in (UserType.ADMIN, UserType.FINANCE)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def parse_wallet_recharge_file(request):
    """
    Parse IIC wallet recharge text file (admin or accounts-in-charge).
    Accepts multipart/form-data with key 'file'. Returns parsed rows with matched user by emp_id.
    Each row: dated, receipt_no, amount, received_from, emp_no, matched_user { id, email, name, emp_id } or null.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can parse wallet recharge files."},
            status=status.HTTP_403_FORBIDDEN,
        )
    file_obj = request.FILES.get("file")
    if not file_obj:
        return Response(
            {"error": "No file provided. Send multipart form with key 'file'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        content = file_obj.read().decode("utf-8", errors="replace")
    except Exception as e:
        return Response(
            {"error": f"Could not read file: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..wallet_recharge_parser import parse_wallet_recharge_file as do_parse
    rows = do_parse(content)
    if not rows:
        return Response(
            {
                "rows": [],
                "message": "No rows parsed. Supported formats: (1) Pipe-delimited (e.g. IIC Wallet-27-02-2026.txt): main row starts with |digit, continuation with |\\s+|; columns 2=date, 3=receipt_no, 4=project_no, 5=amount, 6=payment, 7=received_from. (2) Tab/CSV with header: Receipt No, Amount, Received From or Name. Each data row must have a positive amount.",
            },
            status=status.HTTP_200_OK,
        )
    result = _parser_rows_to_api_result(rows)
    return Response({"rows": result, "count": len(result)}, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def process_wallet_recharge_rows(request):
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can process wallet recharge rows."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        data = request.data
    except Exception:
        data = {}
    rows_payload = data.get("rows") or []
    default_department_id = data.get("default_department_id")
    if not isinstance(rows_payload, list):
        return Response(
            {"error": "Payload must include 'rows' as a list of row objects."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    import_rows = []
    for item in rows_payload:
        if not isinstance(item, dict):
            continue
        r = _parse_recharge_row_for_import(item)
        if r["receipt_no"] and r["amount"] and r["emp_no"]:
            import_rows.append(r)
    if not import_rows:
        return Response(
            {"error": "No valid rows to process (need receipt_no, amount, emp_no)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..wallet_recharge_import import import_wallet_recharge_rows, match_pending_recharge_requests_to_parse_entries

    # Requests consume their receipts first; the direct import then skips anything already used.
    matched_reqs = 0
    match_errs: list = []
    try:
        matched_reqs, match_errs = match_pending_recharge_requests_to_parse_entries()
    except Exception:
        logger.exception("Cash-book request matching failed before import")
    credited, skipped, errors, processed_receipts = import_wallet_recharge_rows(
        import_rows,
        default_department_id=default_department_id,
        dry_run=False,
    )
    if match_errs:
        errors = list(errors) + match_errs[:10]
    return Response({
        "credited": credited,
        "skipped": skipped,
        "errors": errors,
        "processed_receipts": processed_receipts,
        "matched_recharge_requests": matched_reqs,
    }, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def apply_wallet_recharge_parse_entry(request):
    """
    Update one stored parse row by id (e.g. fix Emp No. when no user matched).
    If receipt/date/emp key changes, the old row is replaced. After save, if the row matches a user
    and is not yet processed, runs the same import as \"Credit matched rows\" for that row.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can apply parse entry updates."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    try:
        entry_id = int(data.get("id"))
    except (TypeError, ValueError):
        return Response({"error": "Valid entry id is required."}, status=status.HTTP_400_BAD_REQUEST)
    default_department_id = data.get("default_department_id")

    date_str = data.get("date")
    dated = None
    if date_str:
        from datetime import datetime

        s = str(date_str).strip()[:10]
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                dated = datetime.strptime(s, fmt).date()
                break
            except ValueError:
                continue
    receipt_no = (data.get("receipt_no") or "").strip()
    emp_no = (data.get("emp_no") or "").strip()
    amount = str(data.get("amount") or "").strip()
    if not receipt_no or not emp_no or not amount:
        return Response(
            {"error": "Receipt No., Emp No., and Amount are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    entry = get_object_or_404(WalletRechargeParseEntry, pk=entry_id)
    linked = getattr(entry, "matched_recharge_request", None)
    if linked is not None:
        return Response(
            {"error": f"This cash-book row is already matched to {linked.request_id_display} and cannot be edited."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    new_name = (data.get("name") or "").strip()[:255]
    new_dept = (data.get("department") or "").strip()[:255]
    new_payment = (data.get("payment") or "")[:5000]
    preserved_source_imap_uid = entry.source_imap_uid

    old_key = (entry.receipt_no, entry.dated, entry.emp_no)
    new_key = (receipt_no, dated, emp_no)

    try:
        from django.db import transaction

        with transaction.atomic():
            if old_key != new_key:
                entry.delete()
                entry = WalletRechargeParseEntry.objects.create(
                    receipt_no=receipt_no,
                    dated=dated,
                    emp_no=emp_no,
                    name=new_name,
                    department=new_dept,
                    amount=amount[:50],
                    payment=new_payment,
                    credited_to_project_no=(data.get("credited_to_project_no") or entry.credited_to_project_no or "")[:100],
                    source_imap_uid=preserved_source_imap_uid,
                )
            else:
                entry.name = new_name
                entry.department = new_dept
                entry.amount = amount[:50]
                entry.payment = new_payment
                if "credited_to_project_no" in data:
                    entry.credited_to_project_no = (data.get("credited_to_project_no") or "")[:100]
                entry.save()
    except IntegrityError:
        return Response(
            {"error": "A row with this receipt, date, and employee number already exists."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from ..wallet_recharge_import import import_wallet_recharge_rows, match_pending_recharge_requests_to_parse_entries

    try:
        match_pending_recharge_requests_to_parse_entries()
    except Exception:
        pass

    row = _parse_entry_to_row(entry)
    credited = 0
    skipped = 0
    errors: list = []
    processed_receipts: list = []
    matched_reqs = 0

    if row.get("matched_user") and not row.get("processed"):
        item = {
            "date": row["date"],
            "receipt_no": row["receipt_no"],
            "name": row["name"],
            "emp_no": row["emp_no"],
            "department": row["department"],
            "amount": row["amount"],
            "payment": row["payment"],
        }
        r = _parse_recharge_row_for_import(item)
        if r["receipt_no"] and r["amount"] and r["emp_no"]:
            credited, skipped, errors, processed_receipts = import_wallet_recharge_rows(
                [r],
                default_department_id=default_department_id,
                dry_run=False,
            )
            try:
                matched_reqs, match_errs = match_pending_recharge_requests_to_parse_entries()
                if match_errs:
                    errors = list(errors) + match_errs[:10]
            except Exception:
                pass
            entry.refresh_from_db()
            row = _parse_entry_to_row(entry)

    return Response(
        {
            "row": row,
            "credited": credited,
            "skipped": skipped,
            "errors": errors,
            "processed_receipts": processed_receipts,
            "matched_recharge_requests": matched_reqs,
        },
        status=status.HTTP_200_OK,
    )

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def admin_wallet_eligible_users(request):
    """List users who may have an individual wallet (for manual recharge). Admin or accounts-in-charge."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list eligible users."},
            status=status.HTTP_403_FORBIDDEN,
        )
    search = (request.GET.get("search") or "").strip()
    qs = User.objects.filter(user_type__in=UserType.get_wallet_eligible_codes()).filter(admin_approved=True)
    if search:
        qs = qs.filter(Q(email__icontains=search) | Q(name__icontains=search) | Q(emp_id__icontains=search))
    out = []
    for u in qs.select_related("department").order_by("name", "email")[:200]:
        phone = (u.phone_number or "").strip() or None
        phone2 = (getattr(u, "secondary_phone_number", None) or "").strip() or None
        out.append(
            {
                "id": u.id,
                "name": u.name or "",
                "email": u.email,
                "emp_id": u.emp_id or "",
                "user_type": u.user_type,
                "department_name": getattr(u.department, "name", None) if u.department_id else None,
                "department_id": u.department_id,
                "phone_number": phone,
                "secondary_phone_number": phone2,
                "contact_number": " · ".join(p for p in (phone, phone2) if p) or None,
            }
        )
    return Response({"users": out, "count": len(out)}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_manual_wallet_recharge(request):
    """
    Credit a user's sub-wallet against a receipt, record import + parse rows, notify user (CC office).
    Uses the same duplicate guards as cash-book import. Admin or accounts-in-charge.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can perform manual wallet recharge."},
            status=status.HTTP_403_FORBIDDEN,
        )
    data = request.data if isinstance(request.data, dict) else {}
    uid = data.get("user_id")
    amount_raw = data.get("amount")
    dept_id = data.get("department_id")
    receipt_no = (data.get("receipt_no") or "").strip()
    payment = (data.get("payment") or "Manual admin recharge")[:5000]
    name_override = (data.get("name") or "").strip()
    if not uid or not amount_raw or not dept_id or not receipt_no:
        return Response(
            {"error": "user_id, amount, department_id, and receipt_no are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        amount = Decimal(str(amount_raw).replace(",", "").strip())
    except Exception:
        return Response({"error": "Invalid amount."}, status=status.HTTP_400_BAD_REQUEST)
    if amount <= 0:
        return Response({"error": "Amount must be positive."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        user = User.objects.get(pk=int(uid))
    except (User.DoesNotExist, TypeError, ValueError):
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    if not user.can_have_wallet():
        return Response({"error": "This user is not eligible for an individual wallet."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        dept = Department.objects.get(pk=int(dept_id), department_type=DepartmentType.INTERNAL)
    except (Department.DoesNotExist, TypeError, ValueError):
        return Response({"error": "Invalid internal department."}, status=status.HTTP_400_BAD_REQUEST)
    emp_no = (user.emp_id or "").strip()
    if not emp_no:
        return Response({"error": "User has no employee ID; cannot create parse entry key."}, status=status.HTTP_400_BAD_REQUEST)
    row = _parse_recharge_row_for_import(
        {
            "date": data.get("date") or data.get("dated"),
            "receipt_no": receipt_no,
            "amount": str(amount),
            "name": name_override or (user.name or ""),
            "emp_no": emp_no,
            "department": dept.name or "",
            "payment": payment,
        }
    )
    row["remarks"] = "Manual admin recharge"
    credited, skipped, errors, processed_receipts = wallet_recharge_import.import_wallet_recharge_rows(
        [row], default_department_id=dept.id, dry_run=False
    )
    if credited < 1:
        return Response(
            {
                "error": errors[0] if errors else "Could not credit wallet (duplicate or validation).",
                "errors": errors,
                "skipped": skipped,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    WalletRechargeParseEntry.objects.update_or_create(
        receipt_no=receipt_no,
        dated=row["dated"],
        emp_no=emp_no,
        defaults={
            "name": row["name"][:255],
            "department": (dept.name or "")[:255],
            "amount": f"{amount:,.2f}"[:50],
            "payment": payment,
        },
    )
    entries = WalletRechargeParseEntry.objects.all().order_by("-created_at")
    return Response(
        {
            "message": "Wallet credited and parse entry saved.",
            "processed_receipts": processed_receipts,
            "errors": errors,
            "rows": [_parse_entry_to_row(e) for e in entries],
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def wallet_recharge_target_user_projects(request):
    """Active projects for a faculty user (admin/finance: pick project when creating recharge request)."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list target user projects."},
            status=status.HTTP_403_FORBIDDEN,
        )
    raw_uid = request.GET.get("user_id")
    try:
        uid = int(raw_uid)
    except (TypeError, ValueError):
        return Response({"error": "user_id query parameter is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        target = User.objects.get(pk=uid)
    except User.DoesNotExist:
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    if target.user_type != UserType.FACULTY:
        return Response({"projects": []}, status=status.HTTP_200_OK)
    from ..models import Project

    out = []
    for p in Project.objects.filter(faculty=target, is_active=True).order_by("name"):
        out.append(
            {
                "id": p.id,
                "name": p.name or "",
                "project_code": (p.project_code or "").strip(),
                "agency": (p.agency or "").strip(),
            }
        )
    return Response({"projects": out}, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_wallet_recharge_request_from_unmatched_parse_row(request):
    """
    Wallet Recharge History row has no directory match: ops staff selects the correct user,
    creates a pending WalletRechargeRequest (user OTP treated as verified by staff), emails
    accounts and notifies the faculty like the normal post-OTP flow.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can create a request from a parse row."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    try:
        entry_id = int(data.get("parse_entry_id") or data.get("id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "parse_entry_id (WalletRechargeParseEntry id) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        user_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "user_id (wallet user to notify and request for) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        department_id = int(data.get("department_id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "department_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    project_id_raw = data.get("project_id")
    project_id = None
    if project_id_raw not in (None, ""):
        try:
            project_id = int(project_id_raw)
        except (TypeError, ValueError):
            return Response({"error": "Invalid project_id."}, status=status.HTTP_400_BAD_REQUEST)
    note = (data.get("note") or "").strip()[:500]

    entry = get_object_or_404(WalletRechargeParseEntry, pk=entry_id)
    row = _parse_entry_to_row(entry)
    if row.get("processed"):
        return Response(
            {"error": "This row is already credited via import; create a request only for unprocessed rows."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if row.get("matched_user"):
        return Response(
            {
                "error": "This row already matches a user by employee ID. Use Edit / Credit matched rows, "
                "or the standard recharge flow."
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        amount = Decimal(str((entry.amount or "").replace(",", "").strip()))
    except Exception:
        amount = Decimal("0")
    min_amt = Decimal("100")
    if amount < min_amt:
        return Response(
            {"error": f"Amount must be at least ₹{min_amt} (from parse row)."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        target_user = User.objects.select_related("department").get(pk=user_id)
    except User.DoesNotExist:
        return Response({"error": "Target user not found."}, status=status.HTTP_404_NOT_FOUND)
    if not target_user.can_have_wallet():
        return Response(
            {"error": "Selected user is not eligible for a wallet recharge request."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    wallet = target_user.get_accessible_wallet()
    if not wallet:
        if target_user.can_have_wallet():
            wallet, _ = WalletRepository.get_or_create(target_user)
        if not wallet:
            return Response(
                {"error": "Selected user has no accessible wallet."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    department = resolve_internal_department_for_wallet_recharge(wallet, department_id)
    if department is None:
        return Response(
            {"error": "Invalid department. Choose a department from the recharge list or one that already has a sub-wallet."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from ..models import Project

    project = None
    if target_user.is_faculty():
        if not project_id:
            return Response(
                {"error": "project_id is required for faculty users."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            project = Project.objects.get(id=project_id, faculty=target_user, is_active=True)
        except Project.DoesNotExist:
            return Response(
                {"error": "Invalid project. Project must be active and belong to the selected faculty."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    elif project_id:
        try:
            project = Project.objects.get(id=project_id, faculty=target_user, is_active=True)
        except Project.DoesNotExist:
            return Response(
                {"error": "Invalid project for this user."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    pd_parts = [
        "Manual request from unmatched Wallet Recharge History row.",
        f"Parse entry #{entry.id}; receipt {(entry.receipt_no or '').strip() or '—'};",
        f"file Emp No.: {(entry.emp_no or '').strip() or '—'};",
    ]
    if entry.dated:
        pd_parts.append(f"date {entry.dated.isoformat()};")
    if entry.payment:
        pd_parts.append(f"payment: {(entry.payment or '')[:400]}")
    if note:
        pd_parts.append(f"Staff note: {note}")
    project_details = " ".join(pd_parts).strip()[:4000]

    from django.db import transaction
    from ..wallet_credit_facility import try_activate_credit_facility_after_otp_verify

    with transaction.atomic():
        recharge_request = WalletRechargeRequest.objects.create(
            user=target_user,
            wallet=wallet,
            department=department,
            amount=amount,
            project=project,
            status=WalletRechargeRequestStatus.PENDING,
            user_otp_verified=True,
            project_details=project_details,
            credit_facility_opted_in=False,
        )

    recharge_request.refresh_from_db()
    try_activate_credit_facility_after_otp_verify(recharge_request)
    recharge_request.refresh_from_db()

    _notify_accounts_team_and_faculty_after_recharge_request_user_verified(request, recharge_request)

    request_serializer = WalletRechargeRequestSerializer(recharge_request)
    return Response(
        {
            "request": request_serializer.data,
            "message": (
                "Recharge request created from unmatched parse row. The faculty has been notified; "
                "the accounts team has been emailed."
            ),
        },
        status=status.HTTP_201_CREATED,
    )

@api_view(["GET", "POST", "DELETE"])
@permission_classes([IsAuthenticated])
def wallet_recharge_parse_entries(request):
    """Single endpoint: GET list, POST merge rows, DELETE clear. Admin or accounts-in-charge."""
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can access parse entries."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if request.method == "GET":
        entries = WalletRechargeParseEntry.objects.all().order_by("-created_at")
        rows = [_parse_entry_to_row(e) for e in entries]
        return Response({"rows": rows, "count": len(rows)}, status=status.HTTP_200_OK)
    if request.method == "POST":
        resp, err = _merge_parse_entries_impl(request)
        return err if err is not None else resp
    if request.method == "DELETE":
        deleted, _ = WalletRechargeParseEntry.objects.all().delete()
        return Response({"deleted": deleted, "rows": [], "count": 0}, status=status.HTTP_200_OK)
    return Response(status=status.HTTP_405_METHOD_NOT_ALLOWED)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_list_emails(request):
    """
    List emails via IMAP: last 50 when no subject filter; when subject_filter is set, all matches
    (up to server cap). Admin or accounts-in-charge.
    Body: email, password, host?, port?, use_ssl?, folder?, sender_filter?, subject_filter?
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list emails."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    from ..imap_fetch import list_emails
    emails, error = list_emails(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        folder=config["folder"],
        sender_filter=config["sender_filter"],
        subject_filter=config["subject_filter"],
        max_results=50,
    )
    if error:
        return Response({"error": error, "emails": []}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"emails": emails, "count": len(emails)}, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_fetch_and_parse(request):
    """
    Fetch one email by UID, get first text/csv attachment, parse and return rows. Admin or AIC.
    Body: email, password, host?, port?, use_ssl?, folder?, email_uid (required).
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can fetch email attachments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = data.get("email_uid")
    if not email_uid:
        return Response(
            {"error": "email_uid is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    attachment_index = data.get("attachment_index")
    if attachment_index is not None:
        try:
            attachment_index = int(attachment_index)
        except (TypeError, ValueError):
            attachment_index = None
    from ..imap_fetch import fetch_email_attachment
    content, filename, error = fetch_email_attachment(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        email_uid=str(email_uid).strip(),
        folder=config["folder"],
        attachment_index=attachment_index,
    )
    if error or not content:
        return Response(
            {"error": error or "No attachment content", "rows": [], "count": 0},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..wallet_recharge_parser import parse_wallet_recharge_file as do_parse
    rows = do_parse(content)
    if not rows:
        return Response(
            {
                "rows": [],
                "count": 0,
                "message": "No rows parsed from attachment. Check file format.",
                "attachment_name": filename,
            },
            status=status.HTTP_200_OK,
        )
    result = _parser_rows_to_api_result(rows)
    return Response({
        "rows": result,
        "count": len(result),
        "attachment_name": filename,
    }, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_email_attachments(request):
    """
    List attachments for one email by UID. Admin or accounts-in-charge.
    Body: email, password, host?, port?, use_ssl?, folder?, email_uid (required).
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can list email attachments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = data.get("email_uid")
    if not email_uid:
        return Response(
            {"error": "email_uid is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..imap_fetch import list_attachments_for_email
    attachments, error = list_attachments_for_email(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        email_uid=str(email_uid).strip(),
        folder=config["folder"],
    )
    if error:
        return Response({"error": error, "attachments": []}, status=status.HTTP_400_BAD_REQUEST)
    return Response({"attachments": attachments, "count": len(attachments)}, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_download_attachment(request):
    """
    Download one attachment by email UID and attachment index. Admin only.
    Body: email, password, host?, port?, use_ssl?, folder?, email_uid, attachment_index (0-based).
    Returns JSON: { content_base64, filename } so frontend can trigger download.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can download attachments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = data.get("email_uid")
    attachment_index = data.get("attachment_index")
    if not email_uid:
        return Response({"error": "email_uid is required."}, status=status.HTTP_400_BAD_REQUEST)
    if attachment_index is None:
        return Response({"error": "attachment_index is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        attachment_index = int(attachment_index)
    except (TypeError, ValueError):
        return Response({"error": "attachment_index must be an integer."}, status=status.HTTP_400_BAD_REQUEST)
    import base64
    from ..imap_fetch import get_attachment_content
    content, filename, error = get_attachment_content(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        email_uid=str(email_uid).strip(),
        attachment_index=attachment_index,
        folder=config["folder"],
    )
    if error or content is None:
        return Response(
            {"error": error or "Could not get attachment"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    b64 = base64.b64encode(content).decode("ascii")
    return Response({"content_base64": b64, "filename": filename or "attachment"}, status=status.HTTP_200_OK)

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_imap_delete_email_if_processed(request):
    """
    Delete one mailbox message by UID via IMAP only if every parse entry
    tagged with that source_imap_uid is processed (credited). Admin or accounts-in-charge.
    Clears source_imap_uid on those rows after successful delete.
    """
    if not _is_wallet_recharge_ops_staff(request.user):
        return Response(
            {"error": "Only admin or accounts-in-charge users can delete IMAP messages."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config, err = _imap_config_from_request(request.data)
    if err is not None:
        return err
    try:
        data = request.data if isinstance(request.data, dict) else {}
    except Exception:
        data = {}
    email_uid = (data.get("email_uid") or "").strip()
    if not email_uid:
        return Response({"error": "email_uid is required.", "deleted": False}, status=status.HTTP_400_BAD_REQUEST)
    if not _all_parse_entries_processed_for_imap_uid(email_uid):
        return Response(
            {
                "error": "Not all recharge rows from this email are processed yet, or no rows reference this UID.",
                "deleted": False,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    from ..imap_fetch import delete_email_by_uid

    ok, del_err = delete_email_by_uid(
        host=config["host"],
        port=config["port"],
        use_ssl=config["use_ssl"],
        email_address=config["email_address"],
        password=config["password"],
        folder=config["folder"],
        email_uid=email_uid,
    )
    if not ok:
        return Response({"error": del_err or "Delete failed", "deleted": False}, status=status.HTTP_400_BAD_REQUEST)
    WalletRechargeParseEntry.objects.filter(source_imap_uid=email_uid).update(source_imap_uid=None)
    return Response({"deleted": True, "email_uid": email_uid}, status=status.HTTP_200_OK)


def _matched_user_dict(user: User) -> dict:
    """Serialize matched user for wallet recharge rows (name/department from DB)."""
    dept_name = ""
    if user.department_id and getattr(user, "department", None):
        dept_name = (getattr(user.department, "name", None) or "").strip()
    return {
        "id": user.id,
        "email": user.email,
        "name": (user.name or user.email or "").strip(),
        "emp_id": user.emp_id or "",
        "department_name": dept_name,
    }

def _parser_rows_to_api_result(rows):
    """Convert parser output (list of dicts) to API response rows (date, receipt_no, processed, matched_user, etc.)."""
    result = []
    for row in rows:
        dated_iso = row["dated"].isoformat() if row.get("dated") else None
        amount_val = row.get("amount")
        amount_str = f"{amount_val:,.2f}" if amount_val is not None else ""
        emp_no = row.get("emp_no") or ""
        department = row.get("dept_hint") or ""
        name = row.get("name") or ""
        payment = row.get("payment_details") or ""
        receipt_no = (row.get("receipt_no") or "").strip()
        processed = False
        if receipt_no and emp_no:
            row_dated = row.get("dated")
            qs = WalletRechargeImportRecord.objects.filter(receipt_no=receipt_no, user__emp_id=emp_no)
            if row_dated is not None:
                qs = qs.filter(dated=row_dated)
            processed = qs.exists()
        if receipt_no and not processed:
            processed = wallet_recharge_import.receipt_used_by_request(receipt_no, row.get("dated")) is not None
        matched_user = None
        if emp_no:
            try:
                user = User.objects.select_related("department").get(emp_id=emp_no)
                matched_user = _matched_user_dict(user)
            except User.DoesNotExist:
                pass
        result.append({
            "date": dated_iso,
            "receipt_no": receipt_no,
            "name": name,
            "emp_no": emp_no,
            "department": department,
            "amount": amount_str,
            "payment": payment,
            "credited_to_project_no": (row.get("credited_to_project_no") or "").strip(),
            "processed": processed,
            "matched_user": matched_user,
        })
    return result

def _parse_recharge_row_for_import(item):
    from datetime import datetime
    date_val = item.get("date")
    dated = None
    if date_val:
        if hasattr(date_val, "year"):
            dated = date_val
        else:
            s = str(date_val).strip()[:10]
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    dated = datetime.strptime(s, fmt).date()
                    break
                except ValueError:
                    continue
    amount_raw = str(item.get("amount") or "0").replace(",", "").strip()
    try:
        amount = Decimal(amount_raw) if amount_raw else None
    except Exception:
        amount = None
    name = (item.get("name") or "").strip()
    emp_no = (item.get("emp_no") or "").strip()
    dept = (item.get("department") or "").strip()
    received_from = name
    if emp_no:
        received_from += f" EMP NO-{emp_no}"
    if dept:
        received_from += f" DEPT-OF {dept}"
    return {
        "dated": dated,
        "receipt_no": (item.get("receipt_no") or "").strip(),
        "amount": amount,
        "emp_no": emp_no,
        "dept_hint": dept or None,
        "name": name,
        "payment_details": (item.get("payment") or "").strip(),
        "received_from": received_from.strip(),
        "remarks": "",
    }

def _parse_entry_to_row(entry):
    """Convert WalletRechargeParseEntry to API row with processed and matched_user."""
    row_dated = entry.dated
    emp_no = entry.emp_no or ""
    receipt_no = entry.receipt_no or ""
    qs = WalletRechargeImportRecord.objects.filter(receipt_no=receipt_no, user__emp_id=emp_no)
    if row_dated is not None:
        qs = qs.filter(dated=row_dated)
    linked_req = getattr(entry, "matched_recharge_request", None)
    if linked_req is None:
        linked_req = wallet_recharge_import.receipt_used_by_request(receipt_no, row_dated)
    processed = qs.exists() or linked_req is not None
    matched_user = None
    if emp_no:
        try:
            user = User.objects.select_related("department").get(emp_id=emp_no)
            matched_user = _matched_user_dict(user)
        except User.DoesNotExist:
            pass
    return {
        "id": entry.id,
        "linked_request_id": linked_req.id if linked_req else None,
        "linked_request_display": linked_req.request_id_display if linked_req else "",
        "date": entry.dated.isoformat() if entry.dated else None,
        "receipt_no": receipt_no,
        "name": entry.name or "",
        "emp_no": emp_no,
        "department": entry.department or "",
        "amount": entry.amount or "",
        "payment": entry.payment or "",
        "credited_to_project_no": (getattr(entry, "credited_to_project_no", None) or "").strip(),
        "processed": processed,
        "matched_user": matched_user,
        "source_imap_uid": (entry.source_imap_uid or "").strip(),
    }

def _merge_parse_entries_impl(request):
    """Merge posted rows into stored parse entries. Returns (Response or None, error_response)."""
    try:
        data = request.data
    except Exception:
        data = {}
    rows_payload = data.get("rows") or []
    if not isinstance(rows_payload, list):
        return None, Response(
            {"error": "Payload must include 'rows' as a list of row objects."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    for item in rows_payload:
        if not isinstance(item, dict):
            continue
        date_str = item.get("date")
        dated = None
        if date_str:
            from datetime import datetime
            s = str(date_str).strip()[:10]
            for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
                try:
                    dated = datetime.strptime(s, fmt).date()
                    break
                except ValueError:
                    continue
        receipt_no = (item.get("receipt_no") or "").strip()
        emp_no = (item.get("emp_no") or "").strip()
        if not receipt_no or not emp_no:
            continue
        amount = str(item.get("amount") or "").strip()
        if not amount:
            continue
        defaults = {
            "name": (item.get("name") or "").strip()[:255],
            "department": (item.get("department") or "").strip()[:255],
            "amount": amount[:50],
            "payment": (item.get("payment") or "")[:5000],
            "credited_to_project_no": (item.get("credited_to_project_no") or "").strip()[:100],
        }
        if "source_imap_uid" in item:
            su = (item.get("source_imap_uid") or "").strip()[:32]
            defaults["source_imap_uid"] = su if su else None
        WalletRechargeParseEntry.objects.update_or_create(
            receipt_no=receipt_no,
            dated=dated,
            emp_no=emp_no,
            defaults=defaults,
        )
    try:
        from ..wallet_recharge_import import match_pending_recharge_requests_to_parse_entries

        match_pending_recharge_requests_to_parse_entries()
    except Exception:
        pass
    entries = WalletRechargeParseEntry.objects.all().order_by("-created_at")
    rows = [_parse_entry_to_row(e) for e in entries]
    return Response({"rows": rows, "count": len(rows)}, status=status.HTTP_200_OK), None

def _imap_config_from_request(data):
    """Extract IMAP config dict from request data. Returns (config_dict, error_response)."""
    try:
        d = data if isinstance(data, dict) else {}
    except Exception:
        d = {}
    email_address = (d.get("email") or getattr(settings, "IMAP_USER", "") or "").strip()
    password = d.get("password") or getattr(settings, "IMAP_PASSWORD", "") or ""
    host = (d.get("host") or getattr(settings, "IMAP_HOST", "") or "mapi.iitr.ac.in").strip()
    if not host:
        return None, Response({"error": "IMAP host is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not email_address:
        return None, Response({"error": "Email is required (or set IMAP_USER)."}, status=status.HTTP_400_BAD_REQUEST)
    if not password:
        return None, Response({"error": "Password is required (or set IMAP_PASSWORD)."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        port = int(d.get("port") or getattr(settings, "IMAP_PORT", 993) or 993)
    except (TypeError, ValueError):
        port = 993
    use_ssl = d.get("use_ssl", True) if d.get("use_ssl") is not False else True
    if d.get("use_ssl") is None and hasattr(settings, "IMAP_USE_SSL"):
        use_ssl = bool(settings.IMAP_USE_SSL)
    folder = (d.get("folder") or getattr(settings, "IMAP_MAILBOX", "INBOX") or "INBOX").strip() or "INBOX"
    # Default: cash-book mails from SRIC bills
    sender_filter = (d.get("sender_filter") or "").strip() or "bills@sric.iitr.ac.in"
    subject_filter = (d.get("subject_filter") or "").strip() or None
    return {
        "host": host,
        "port": port,
        "use_ssl": use_ssl,
        "email_address": email_address,
        "password": password,
        "folder": folder,
        "sender_filter": sender_filter,
        "subject_filter": subject_filter,
    }, None

def _all_parse_entries_processed_for_imap_uid(imap_uid: str) -> bool:
    u = (imap_uid or "").strip()
    if not u:
        return False
    qs = WalletRechargeParseEntry.objects.filter(source_imap_uid=u)
    if not qs.exists():
        return False
    for entry in qs:
        row = _parse_entry_to_row(entry)
        if not row.get("processed"):
            return False
    return True

