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


_SRIC_STATUSES = {
    "credited": "Credited",
    "awaiting_credit": "Ready to credit",
    "needs_review": "Needs review",
    "duplicate": "Duplicate",
    "failed": "Failed",
    "rejected": "Rejected",
}


@register("sric-wallet-recharges")
def sric_wallet_recharges(request):
    rows, _ = collect_rows(request, "admin-sric-wallet-recharges", page_size=200, page_param="page",
                           limit_param="page_size")
    columns = [
        SNO,
        C("reference", "Reference", width=0.9),
        C("email_date", "Email date (IST)", spec.DATETIME, 1.15),
        C("financial_year", "FY", width=0.7),
        C("ledger_id", "Ledger ID", width=1.3),
        C("project_number", "Project number", width=1.3),
        C("pi_name", "PI name", width=1.3),
        C("employee_id", "Employee ID", width=0.9),
        C("receiver_label", "Receiver", width=0.8),
        C("amount", "Amount (₹)", spec.CURRENCY, 1.0, total=True),
        C("status_display", "Status", width=1.0),
        C("review_message", "Review reason", width=1.6),
        C("matched_user", "Credited to", width=1.4, value=lambda r: (r.get("matched_user") or {}).get("name", "")),
        C("credited_at", "Credited (IST)", spec.DATETIME, 1.15),
        C("fund_receipt", "Fund receipt", width=1.1,
          value=lambda r: ("Verified" if r.get("fund_receipt_verified") else "Not verified") if r.get("status") == "credited" else ""),
        C("fund_receipt_verification_remarks", "Verification remarks", width=1.4),
    ]
    kpis = [
        spec.Kpi("Rows", len(rows), spec.INTEGER),
        spec.Kpi("Credited amount", _amount_sum(rows, status="credited"), spec.CURRENCY),
        spec.Kpi("Need action", sum(1 for r in rows if r.get("status") in ("needs_review", "awaiting_credit", "failed")),
                 spec.INTEGER),
    ]
    filters = filter_pairs(request, [
        ("status", "Status", _SRIC_STATUSES),
        ("financial_year", "Financial year", "text"),
        ("receiver", "Receiver", "text"),
        ("verified", "Fund receipt", {"true": "Verified", "false": "Not verified"}),
        ("date_from", "From", "date"),
        ("date_to", "To", "date"),
        ("search", "Search", "text"),
    ])
    table = spec.Table("rows", "SRIC wallet recharges", columns, numbered(rows),
                       empty_message="No SRIC wallet recharges match these filters.")
    return make_document(request, title="SRIC Wallet Recharges", slug="sric-wallet-recharges",
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


_BALANCE_STATES = {"positive": "Positive", "zero": "Zero", "negative": "Negative", "zero_or_negative": "Zero or negative"}
_OWNER_STATUS = {"active": "Active", "inactive": "Inactive"}
_TXN_TYPES = {"credit": "Credit", "debit": "Debit"}


def _owner_type_label(raw: str) -> str:
    from iic_booking.users.models import UserType

    labels = dict(UserType.get_choices())
    return ", ".join(str(labels.get(v.strip(), v.strip())) for v in raw.split(","))


def _sub_wallets_text(row) -> str:
    return "; ".join(f"{s['department_name']}: ₹{Decimal(s['balance']):,.2f}" for s in row.get("sub_wallets") or [])


def _ledger_choice(key: str):
    def resolve(raw: str) -> str:
        from iic_booking.users import admin_wallet_ledger as ledger

        options = {"category": ledger.category_options(), "performer": ledger.PERFORMER_OPTIONS}[key]
        labels = {o["value"]: o["label"] for o in options}
        return ", ".join(labels.get(v.strip(), v.strip()) for v in raw.split(","))

    return resolve


@register("admin-wallet-owners")
def admin_wallet_owners(request):
    rows, first = collect_rows(request, "admin-wallet-ledger-owners", page_size=500, page_param="page",
                               limit_param="page_size")
    summary = (first or {}).get("summary") or {}
    columns = [
        SNO,
        C("name", "Wallet owner", width=1.5),
        C("employee_id", "Employee / enrolment no.", width=1.0),
        C("user_type_label", "Category", width=1.1),
        C("department_name", "Department", width=1.4),
        C("sub_wallets", "Sub-wallets and balances", width=2.4, value=_sub_wallets_text),
        C("total_balance", "Total balance (₹)", spec.CURRENCY, 1.0, total=True),
        C("linked_students", "Linked students", spec.INTEGER, 0.7),
        C("status", "Status", width=0.7, value=lambda r: _OWNER_STATUS.get(r.get("status"), r.get("status"))),
        C("last_transaction_at", "Last transaction (IST)", spec.DATETIME, 1.15),
    ]
    kpis = [
        spec.Kpi("Wallet owners", summary.get("owners", len(rows)), spec.INTEGER),
        spec.Kpi("Total balance", to_number(summary.get("total_balance")) or 0, spec.CURRENCY),
        spec.Kpi("Negative balance", summary.get("negative_owners", 0), spec.INTEGER),
        spec.Kpi("Zero balance", summary.get("zero_owners", 0), spec.INTEGER),
    ]
    filters = filter_pairs(request, [
        ("search", "Search", "text"),
        ("department", "Department", "department"),
        ("owner_type", "Category", _owner_type_label),
        ("sub_wallet_department", "Sub-wallet", "department"),
        ("balance_state", "Balance", _BALANCE_STATES),
        ("balance_min", "Balance from (₹)", "text"),
        ("balance_max", "Balance up to (₹)", "text"),
        ("status", "Status", _OWNER_STATUS),
        ("has_students", "Linked students", {"yes": "Has linked students", "no": "No linked students"}),
        ("activity_from", "Activity from", "date"),
        ("activity_to", "Activity to", "date"),
    ])
    table = spec.Table("owners", "Wallet owners", columns, numbered(rows),
                       note="Email addresses and phone numbers are left out of exports.",
                       empty_message="No wallet owners match these filters.")
    return make_document(request, title="Wallet Owners", slug="wallet-owners", tables=[table], filters=filters,
                         kpis=kpis, landscape=True)


@register("admin-wallet-transactions")
def admin_wallet_transactions(request):
    rows, first = collect_rows(request, "admin-wallet-ledger-transactions", page_size=500, page_param="page",
                               limit_param="page_size")
    summary = (first or {}).get("summary") or {}
    single_owner = bool((request.query_params.get("owner") or "").strip())
    columns = [
        SNO,
        C("created_at", "Date & time (IST)", spec.DATETIME, 1.15),
        C("id", "Transaction ID", spec.INTEGER, 0.8),
    ]
    if not single_owner:
        columns += [
            C("owner_name", "Wallet owner", width=1.4),
            C("owner_department", "Owner department", width=1.2),
        ]
    columns += [
        C("transaction_type", "Type", width=0.6, value=lambda r: _TXN_TYPES.get(r.get("transaction_type"), "")),
        C("category_label", "Source", width=1.1),
        C("booking_code", "Booking", width=1.1),
        C("department_name", "Sub-wallet", width=1.2),
        C("credit", "Credit (₹)", spec.CURRENCY, 0.9, total=True,
          value=lambda r: r.get("amount") if r.get("transaction_type") == "credit" else None),
        C("debit", "Debit (₹)", spec.CURRENCY, 0.9, total=True,
          value=lambda r: r.get("amount") if r.get("transaction_type") == "debit" else None),
        C("balance_after", "Balance after (₹)", spec.CURRENCY, 1.0),
        C("performed_by", "Performed by", width=1.2),
        C("description", "Description", width=2.4),
        C("remarks", "Remarks", width=1.6),
    ]
    kpis = [
        spec.Kpi("Transactions", summary.get("transactions", len(rows)), spec.INTEGER),
        spec.Kpi("Total credits", to_number(summary.get("total_credits")) or 0, spec.CURRENCY),
        spec.Kpi("Total debits", to_number(summary.get("total_debits")) or 0, spec.CURRENCY),
        spec.Kpi("Net", to_number(summary.get("net")) or 0, spec.CURRENCY),
    ]
    filters = filter_pairs(request, [
        ("owner", "Wallet owner", "user"),
        ("owner_department", "Owner department", "department"),
        ("owner_type", "Owner category", _owner_type_label),
        ("sub_wallet_department", "Sub-wallet", "department"),
        ("type", "Type", _TXN_TYPES),
        ("category", "Source", _ledger_choice("category")),
        ("performer", "Performed by", _ledger_choice("performer")),
        ("date_from", "From", "date"),
        ("date_to", "To", "date"),
        ("amount_min", "Amount from (₹)", "text"),
        ("amount_max", "Amount up to (₹)", "text"),
        ("booking", "Booking", "text"),
        ("related_user", "Booking user", "user"),
        ("search", "Search", "text"),
    ])
    title = "Wallet Transactions"
    subtitle = ""
    if single_owner and rows:
        subtitle = f"{rows[0].get('owner_name') or ''} — {rows[0].get('owner_department') or ''}".strip(" —")
    table = spec.Table("transactions", "Wallet transactions", columns, numbered(rows),
                       empty_message="No wallet transactions match these filters.")
    return make_document(request, title=title, slug="wallet-transactions", subtitle=subtitle, tables=[table],
                         filters=filters, kpis=kpis, landscape=True)


_LINK_STATUS = {
    "linked": "Linked",
    "pending": "Pending approval",
    "declined": "Declined",
    "removed": "Removed by owner",
    "cancelled": "Withdrawn by student",
}


def _limit_text(row: dict) -> str:
    if not row.get("spending_limit_enabled"):
        return ""
    parts = []
    if row.get("weekly_limit") is not None:
        parts.append(f"Week ₹{row.get('week_spent') or '0.00'} of ₹{row['weekly_limit']}")
    if row.get("monthly_limit") is not None:
        parts.append(f"Month ₹{row.get('month_spent') or '0.00'} of ₹{row['monthly_limit']}")
    return "; ".join(parts)


@register("admin-wallet-linked-students")
def admin_wallet_linked_students(request):
    rows, first = collect_rows(request, "admin-wallet-ledger-linked-students", page_size=None)
    data = first or {}
    summary = data.get("summary") or {}
    has_range = bool(request.query_params.get("date_from") or request.query_params.get("date_to"))
    columns = [
        SNO,
        C("name", "Student", width=1.5),
        C("enrollment", "Enrolment / employee no.", width=1.0),
        C("department_name", "Department", width=1.3),
        C("user_type_label", "Category", width=1.0),
        C("status", "Link status", width=0.9, value=lambda r: _LINK_STATUS.get(r.get("status"), r.get("status"))),
        C("requested_at", "Requested (IST)", spec.DATETIME, 1.1),
        C("responded_at", "Linked / changed (IST)", spec.DATETIME, 1.1),
        C("sub_wallets", "Books against", width=1.4,
          value=lambda r: ", ".join(s.get("department_name", "") for s in r.get("sub_wallets") or [])),
        C("limits", "Spending limits (used of limit)", width=1.8, value=_limit_text),
        C("total_spent", "Spent from wallet (₹)", spec.CURRENCY, 1.0, total=True),
    ]
    if has_range:
        columns.append(C("range_spent", "Spent in period (₹)", spec.CURRENCY, 1.0, total=True))
    columns.append(C("last_booking_at", "Last booking (IST)", spec.DATETIME, 1.1))
    kpis = [
        spec.Kpi("Linked", summary.get("linked", 0), spec.INTEGER),
        spec.Kpi("Pending", summary.get("pending", 0), spec.INTEGER),
        spec.Kpi("Spent from wallet", to_number(summary.get("total_spent")) or 0, spec.CURRENCY),
    ]
    filters = filter_pairs(request, [
        ("owner", "Wallet owner", "user"),
        ("status", "Link status", _LINK_STATUS),
        ("date_from", "Spent from", "date"),
        ("date_to", "Spent to", "date"),
        ("search", "Search", "text"),
    ])
    table = spec.Table("students", "Linked students", columns, numbered(rows),
                       note="Email addresses are left out of exports. Spend is booking charges on this wallet minus refunds.",
                       empty_message="No linked students match these filters.")
    return make_document(request, title="Linked Students", slug="wallet-linked-students",
                         subtitle=data.get("owner_name") or "", tables=[table], filters=filters, kpis=kpis,
                         landscape=True)
