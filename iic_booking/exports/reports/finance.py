"""Wallet and finance lists: wallet recharge requests and wallet withdrawal requests."""

from __future__ import annotations

from decimal import Decimal

from .. import spec
from ..bridge import collect_rows
from ..registry import register
from ..values import to_number
from .booking_activity import humanize
from .common import SNO
from .common import filter_pairs
from .common import make_document
from .common import numbered

C = spec.Column

_REQUEST_STATUSES = {"PENDING": "Pending", "APPROVED": "Approved", "REJECTED": "Rejected", "CANCELLED": "Cancelled",
                     "COMPLETED": "Completed"}
_RECHARGE_MODES = {"project_grant": "Project Grant", "direct_cash_deposit": "Cash / Bank Transfer"}


def _amount_sum(rows, *, status=None) -> Decimal:
    total = Decimal("0")
    for row in rows:
        if status and row.get("status") != status:
            continue
        number = to_number(row.get("amount"))
        if number is not None:
            total += Decimal(str(number))
    return total


def _status(row) -> str:
    return row.get("status_display") or _REQUEST_STATUSES.get(row.get("status"), humanize(row.get("status")))


def _grant_code(row) -> str:
    return row.get("project_grant_code") or row.get("project_code") or row.get("department_grant_code") or ""


def _fund_receipt(row) -> str:
    if row.get("recharge_mode") != "project_grant":
        return ""
    if row.get("fund_receipt_verified"):
        by = row.get("fund_receipt_verified_by_name") or ""
        return f"Verified by {by}" if by else "Verified"
    return "Not verified"


@register("wallet-recharge-requests")
def wallet_recharge_requests(request):
    params = request.query_params.copy()
    params["ordering"] = params.get("ordering") or "-created_at"
    rows, _ = collect_rows(request, "admin-walletrechargerequest-list", page_size=200, page_param="page",
                           limit_param="page_size", params=params)
    columns = [
        SNO,
        C("request_id", "Request ID", width=1.0),
        C("transaction_number", "Transaction no.", width=1.2),
        C("created_at", "Requested (IST)", spec.DATETIME, 1.15),
        C("user_name", "User", width=1.3),
        C("user_email", "Email", width=1.6),
        C("user_emp_id", "Employee ID", width=0.9),
        C("department_name", "Department", width=1.4),
        C("recharge_mode", "Mode", width=1.1,
          value=lambda r: _RECHARGE_MODES.get(r.get("recharge_mode"), humanize(r.get("recharge_mode")))),
        C("grant_code", "Grant / project code", width=1.2, value=_grant_code),
        C("amount", "Amount (₹)", spec.CURRENCY, 1.0, total=True),
        C("status", "Status", width=0.9, value=_status),
        C("fund_receipt", "Fund receipt", width=1.2, value=_fund_receipt),
        C("cashbook_receipt_no", "Cash-book receipt", width=1.0),
        C("utr_reference", "UTR", width=1.0),
        C("responded_at", "Responded (IST)", spec.DATETIME, 1.15),
        C("response_message", "Remarks", width=1.8,
          value=lambda r: r.get("response_message") or r.get("rejection_reason_text") or ""),
    ]
    kpis = [
        spec.Kpi("Requests", len(rows), spec.INTEGER),
        spec.Kpi("Total requested", _amount_sum(rows), spec.CURRENCY),
        spec.Kpi("Approved amount", _amount_sum(rows, status="APPROVED"), spec.CURRENCY),
        spec.Kpi("Pending", sum(1 for r in rows if r.get("status") == "PENDING"), spec.INTEGER),
    ]
    filters = filter_pairs(request, [
        ("status", "Status", _REQUEST_STATUSES),
        ("recharge_mode", "Mode", _RECHARGE_MODES),
        ("fund_receipt_verified", "Fund receipt", {"true": "Verified", "false": "Not verified"}),
        ("department", "Department", "department"),
        ("cashbook", "SRIC cash-book", "text"),
        ("project_grant", "Project grant", "text"),
        ("date_from", "From", "date"),
        ("date_to", "To", "date"),
        ("overdue", "Overdue only", {"1": "Yes"}),
        ("search", "Search", "text"),
    ])
    table = spec.Table("requests", "Wallet recharge requests", columns, numbered(rows),
                       empty_message="No recharge requests match these filters.")
    return make_document(request, title="Wallet Recharge Requests", slug="wallet-recharge-requests",
                         tables=[table], filters=filters, kpis=kpis, landscape=True)


@register("wallet-withdrawal-requests")
def wallet_withdrawal_requests(request):
    rows, _ = collect_rows(request, "admin-wallet-withdrawal-requests-list", page_size=None)
    columns = [
        SNO,
        C("id", "Request no.", spec.INTEGER, 0.7),
        C("created_at", "Requested (IST)", spec.DATETIME, 1.15),
        C("user_name", "User", width=1.3),
        C("user_email", "Email", width=1.6),
        C("amount", "Amount (₹)", spec.CURRENCY, 1.0, total=True),
        C("status", "Status", width=0.9, value=_status),
        C("user_note", "User note", width=1.6),
        C("approved_by_email", "Approved by", width=1.4),
        C("utr_reference", "UTR", width=1.0),
        C("responded_at", "Responded (IST)", spec.DATETIME, 1.15),
        C("completed_at", "Completed (IST)", spec.DATETIME, 1.15),
        C("response_message", "Remarks", width=1.6),
    ]
    kpis = [
        spec.Kpi("Requests", len(rows), spec.INTEGER),
        spec.Kpi("Total requested", _amount_sum(rows), spec.CURRENCY),
        spec.Kpi("Completed amount", _amount_sum(rows, status="COMPLETED"), spec.CURRENCY),
        spec.Kpi("Pending", sum(1 for r in rows if r.get("status") == "PENDING"), spec.INTEGER),
    ]
    filters = filter_pairs(request, [("status", "Status", _REQUEST_STATUSES)])
    table = spec.Table("requests", "Wallet withdrawal requests", columns, numbered(rows),
                       note="Bank account details are left out of exports.",
                       empty_message="No withdrawal requests match these filters.")
    return make_document(request, title="Wallet Withdrawal Requests", slug="wallet-withdrawal-requests",
                         tables=[table], filters=filters, kpis=kpis, landscape=True)
