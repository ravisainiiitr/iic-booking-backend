"""Wallet ledger for the Main Administrator: wallet owners, every sub-wallet transaction, manual credit / debit.

Transactions carry only a type, an amount and a description, so the source (recharge, booking charge, refund …)
and who performed it are derived here from the linked records and the description wording used by each flow.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import Case, CharField, Count, DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, Value, When
from django.db.models.functions import Coalesce
from django.utils import timezone

from iic_booking.users.models import Department, DepartmentType, User, UserType
from iic_booking.users.models.wallet import (
    SubWallet,
    SubWalletTransaction,
    Wallet,
    WalletJoinRequest,
    WalletJoinRequestStatus,
)
from iic_booking.users.models.wallet_admin_adjustment import (
    WalletAdminAdjustment,
    WalletAdminAdjustmentDirection,
    WalletAdminAdjustmentReason,
)

logger = logging.getLogger(__name__)

MAX_ADJUSTMENT_AMOUNT = Decimal("10000000.00")
ZERO = Decimal("0.00")
CLIENT_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9_.:-]{8,64}")
_MONEY = DecimalField(max_digits=14, decimal_places=2)


def is_main_admin(user) -> bool:
    return bool(
        user is not None
        and getattr(user, "is_authenticated", False)
        and (getattr(user, "is_superuser", False) or getattr(user, "user_type", None) == UserType.ADMIN)
    )


class LedgerError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, extra: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra or {}


# --- Transaction source / performer -------------------------------------------------------------------------


def _starts(*prefixes: str) -> Q:
    q = Q()
    for p in prefixes:
        q |= Q(description__istartswith=p)
    return q


def _contains(*parts: str) -> Q:
    q = Q()
    for p in parts:
        q |= Q(description__icontains=p)
    return q


def category_rules() -> list[tuple[str, str, Q | None]]:
    """(key, label, condition) in priority order; the first matching rule names the source."""
    from iic_booking.users.legacy_ledger.opening_balance import LEGACY_CREDIT_PREFIXES

    return [
        (
            "manual_admin",
            "Manual admin credit / debit",
            Q(admin_adjustment__isnull=False) | _starts("Admin credit", "Admin debit", "Bulk admin"),
        ),
        ("direct_recharge", "Direct wallet recharge", Q(direct_recharge__isnull=False) | _starts("Direct wallet recharge")),
        ("sric_recharge", "SRIC wallet recharge", Q(sric_wallet_recharge__isnull=False) | _starts("SRIC wallet recharge")),
        ("legacy_sync", "Old portal sync", _starts(*LEGACY_CREDIT_PREFIXES)),
        (
            "credit_facility",
            "Wallet credit facility",
            _starts("WALLET_CREDIT", "CREDIT_REPAYMENT", "Auto-approved credit", "Credit recovered"),
        ),
        ("refund", "Refund", _contains("refund") | _starts("Demonstration charge waived")),
        ("extra_charge", "Extra charge", _starts("Additional charge") | _contains("charge recalculation")),
        ("booking_charge", "Booking charge", _starts("Booking #", "Urgent approval:")),
        ("training_charge", "Training / demonstration", _starts("Demonstration charge") | _contains("training")),
        ("recharge", "Recharge", _contains("recharge") | _starts("Offline UTR")),
        ("transfer", "Transfer", _starts("Wallet transfer", "Transfer to", "Transfer from")),
        ("withdrawal", "Withdrawal", _starts("Wallet withdrawal")),
        ("other", "Other", None),
    ]


def category_options() -> list[dict[str, str]]:
    return [{"value": key, "label": label} for key, label, _ in category_rules()]


PERFORMER_OPTIONS = [
    {"value": "admin", "label": "Administrator"},
    {"value": "user", "label": "Wallet owner / user"},
    {"value": "system", "label": "System"},
]


def annotate_source(qs):
    from iic_booking.users.legacy_ledger.opening_balance import ADMIN_SYNC_PREFIX

    whens = [When(cond, then=Value(key)) for key, _, cond in category_rules() if cond is not None]
    qs = qs.annotate(category=Case(*whens, default=Value("other"), output_field=CharField()))
    admin_q = Q(category__in=("manual_admin", "direct_recharge")) | Q(description__istartswith=ADMIN_SYNC_PREFIX)
    user_q = Q(category__in=("booking_charge", "extra_charge", "training_charge", "transfer", "withdrawal")) | Q(
        description__istartswith="Recharge via Razorpay"
    )
    return qs.annotate(
        performer=Case(
            When(admin_q, then=Value("admin")),
            When(user_q, then=Value("user")),
            default=Value("system"),
            output_field=CharField(),
        )
    )


_REF_RE = re.compile(r"\bRef:\s*(\S+)", re.I)
_NUMERIC_BOOKING_RE = re.compile(r"Booking #(\d+)\s*[-–]")


def booking_reference(description: str) -> tuple[str | None, int | None]:
    """(public booking code, booking pk) found in a transaction description."""
    desc = description or ""
    m = _REF_RE.search(desc)
    if m:
        return m.group(1).strip().rstrip(",;"), None
    m = _NUMERIC_BOOKING_RE.search(desc)
    if m:
        return None, int(m.group(1))
    return None, None


# --- Parsing helpers ----------------------------------------------------------------------------------------


def _int(value) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _date(value) -> date | None:
    try:
        return date.fromisoformat(str(value or "").strip()[:10])
    except ValueError:
        return None


def _csv(params, key: str) -> list[str]:
    out: list[str] = []
    for raw in params.getlist(key) if hasattr(params, "getlist") else [params.get(key)]:
        out.extend(p.strip() for p in str(raw or "").split(",") if p.strip())
    return out


def _page(params, default_size: int = 25) -> tuple[int, int]:
    page = max(1, _int(params.get("page")) or 1)
    size = _int(params.get("page_size")) or default_size
    return page, max(1, min(size, 500))


def _money(value) -> str:
    return str(Decimal(value or 0).quantize(Decimal("0.01")))


def _display_name(user) -> str:
    from iic_booking.users.display import get_user_display_name

    if user is None:
        return ""
    return get_user_display_name(user) or user.email or ""


def _type_label(code: str | None) -> str:
    return dict(UserType.get_choices()).get(code or "", code or "")


# --- Wallet owners ------------------------------------------------------------------------------------------


OWNER_ORDERING = {
    "name": ("user__name", "user__email"),
    "balance": ("total_balance_value", "user__name"),
    "last_activity": ("last_transaction_at", "user__name"),
    "students": ("linked_students", "user__name"),
    "department": ("user__department__name", "user__name"),
}


def owners_queryset():
    balance = (
        SubWallet.objects.filter(wallet=OuterRef("pk"))
        .order_by()
        .values("wallet")
        .annotate(s=Sum("balance"))
        .values("s")[:1]
    )
    subs = SubWallet.objects.filter(wallet=OuterRef("pk")).order_by().values("wallet").annotate(c=Count("pk")).values("c")[:1]
    last_txn = (
        SubWalletTransaction.objects.filter(sub_wallet__wallet=OuterRef("pk")).order_by("-created_at").values("created_at")[:1]
    )
    students = (
        WalletJoinRequest.objects.filter(faculty=OuterRef("user_id"), status=WalletJoinRequestStatus.APPROVED)
        .order_by()
        .values("faculty")
        .annotate(c=Count("student", distinct=True))
        .values("c")[:1]
    )
    return Wallet.objects.select_related("user", "user__department").annotate(
        total_balance_value=Coalesce(Subquery(balance, output_field=_MONEY), Value(ZERO, output_field=_MONEY)),
        sub_wallet_count=Coalesce(Subquery(subs), Value(0)),
        last_transaction_at=Subquery(last_txn),
        linked_students=Coalesce(Subquery(students), Value(0)),
    )


def filter_owners(qs, params):
    search = (params.get("search") or "").strip()
    if search:
        qs = qs.filter(
            Q(user__name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(user__emp_id__icontains=search)
            | Q(user__department__name__icontains=search)
        )
    dept = _int(params.get("department"))
    if dept:
        qs = qs.filter(user__department_id=dept)
    types = _csv(params, "owner_type")
    if types:
        qs = qs.filter(user__user_type__in=types)
    sw_dept = _int(params.get("sub_wallet_department"))
    if sw_dept:
        qs = qs.filter(Exists(SubWallet.objects.filter(wallet=OuterRef("pk"), department_id=sw_dept)))
    state = (params.get("balance_state") or "").strip()
    if state == "positive":
        qs = qs.filter(total_balance_value__gt=0)
    elif state == "zero":
        qs = qs.filter(total_balance_value=0)
    elif state == "negative":
        qs = qs.filter(total_balance_value__lt=0)
    elif state == "zero_or_negative":
        qs = qs.filter(total_balance_value__lte=0)
    lo, hi = _decimal(params.get("balance_min")), _decimal(params.get("balance_max"))
    if lo is not None:
        qs = qs.filter(total_balance_value__gte=lo)
    if hi is not None:
        qs = qs.filter(total_balance_value__lte=hi)
    has_students = (params.get("has_students") or "").strip().lower()
    if has_students == "yes":
        qs = qs.filter(linked_students__gt=0)
    elif has_students == "no":
        qs = qs.filter(linked_students=0)
    status = (params.get("status") or "").strip()
    if status == "active":
        qs = qs.filter(user__is_active=True)
    elif status == "inactive":
        qs = qs.filter(user__is_active=False)
    a_from, a_to = _date(params.get("activity_from")), _date(params.get("activity_to"))
    if a_from or a_to:
        txns = SubWalletTransaction.objects.filter(sub_wallet__wallet=OuterRef("pk"))
        if a_from:
            txns = txns.filter(created_at__date__gte=a_from)
        if a_to:
            txns = txns.filter(created_at__date__lte=a_to)
        qs = qs.filter(Exists(txns))
    elif (params.get("activity") or "").strip() == "none":
        qs = qs.filter(last_transaction_at__isnull=True)
    return qs


def order_owners(qs, ordering: str):
    key = (ordering or "name").strip()
    desc = key.startswith("-")
    fields = OWNER_ORDERING.get(key.lstrip("-"), OWNER_ORDERING["name"])
    first = F(fields[0]).desc(nulls_last=True) if desc else F(fields[0]).asc(nulls_last=True)
    return qs.order_by(first, *fields[1:], "pk")


def serialize_owner(wallet: Wallet, sub_wallets: list[SubWallet], *, s_no: int | None = None) -> dict[str, Any]:
    user = wallet.user
    dept = user.department if user.department_id else None
    return {
        "s_no": s_no,
        "owner_id": user.pk,
        "wallet_id": wallet.pk,
        "name": _display_name(user),
        "email": user.email or "",
        "employee_id": user.emp_id or "",
        "user_type": user.user_type,
        "user_type_label": _type_label(user.user_type),
        "department_id": user.department_id,
        "department_name": dept.name if dept else "",
        "total_balance": _money(getattr(wallet, "total_balance_value", None) or ZERO),
        "sub_wallets": [
            {
                "id": sw.pk,
                "department_id": sw.department_id,
                "department_name": sw.department.name,
                "department_code": sw.department.code or "",
                "balance": _money(sw.balance),
            }
            for sw in sub_wallets
        ],
        "linked_students": int(getattr(wallet, "linked_students", 0) or 0),
        "status": "active" if user.is_active else "inactive",
        "last_transaction_at": wallet.last_transaction_at.isoformat() if getattr(wallet, "last_transaction_at", None) else None,
    }


def list_owners(params) -> dict[str, Any]:
    qs = filter_owners(owners_queryset(), params)
    page, size = _page(params)
    total = qs.count()
    totals = SubWallet.objects.filter(wallet__in=qs.values("pk")).aggregate(s=Sum("balance"))
    summary = {
        "owners": total,
        "total_balance": _money(totals["s"] or ZERO),
        "negative_owners": qs.filter(total_balance_value__lt=0).count(),
        "zero_owners": qs.filter(total_balance_value=0).count(),
    }
    rows = list(order_owners(qs, params.get("ordering") or "name")[(page - 1) * size : page * size])
    subs: dict[int, list[SubWallet]] = defaultdict(list)
    for sw in SubWallet.objects.filter(wallet__in=[w.pk for w in rows]).select_related("department").order_by("department__name"):
        subs[sw.wallet_id].append(sw)
    start = (page - 1) * size
    return {
        "count": total,
        "page": page,
        "page_size": size,
        "summary": summary,
        "results": [serialize_owner(w, subs[w.pk], s_no=start + i + 1) for i, w in enumerate(rows)],
    }


def filter_options() -> dict[str, Any]:
    used_types = set(Wallet.objects.values_list("user__user_type", flat=True).distinct())
    owner_depts = Department.objects.filter(pk__in=Wallet.objects.values("user__department_id")).order_by("name")
    sub_depts = Department.objects.filter(
        department_type=DepartmentType.INTERNAL, pk__in=SubWallet.objects.values("department_id")
    ).order_by("name")
    return {
        "owner_types": [{"value": c, "label": str(label)} for c, label in UserType.get_choices() if c in used_types],
        "departments": [{"value": str(d.pk), "label": d.name} for d in owner_depts],
        "sub_wallet_departments": [{"value": str(d.pk), "label": d.name} for d in sub_depts],
        "categories": category_options(),
        "performers": PERFORMER_OPTIONS,
        "reasons": [{"value": v, "label": str(label)} for v, label in WalletAdminAdjustmentReason.choices],
        "max_amount": str(MAX_ADJUSTMENT_AMOUNT),
    }


def owner_detail(owner_id) -> dict[str, Any]:
    wallet = owners_queryset().filter(user_id=_int(owner_id)).first()
    if wallet is None:
        raise LedgerError("WALLET_NOT_FOUND", "This user has no wallet.", status=404)
    user = wallet.user
    subs = list(SubWallet.objects.filter(wallet=wallet).select_related("department").order_by("department__name"))
    stats = {
        row["sub_wallet_id"]: row
        for row in SubWalletTransaction.objects.filter(sub_wallet__wallet=wallet)
        .order_by()
        .values("sub_wallet_id")
        .annotate(
            count=Count("pk"),
            credits=Coalesce(Sum("amount", filter=Q(transaction_type="credit")), Value(ZERO, output_field=_MONEY)),
            debits=Coalesce(Sum("amount", filter=Q(transaction_type="debit")), Value(ZERO, output_field=_MONEY)),
        )
    }
    data = serialize_owner(wallet, subs)
    for item in data["sub_wallets"]:
        s = stats.get(item["id"], {})
        item["transaction_count"] = s.get("count", 0)
        item["total_credits"] = _money(s.get("credits") or ZERO)
        item["total_debits"] = _money(s.get("debits") or ZERO)
    phones = [p for p in ((user.phone_number or "").strip(), (getattr(user, "secondary_phone_number", "") or "").strip()) if p]
    students = (
        WalletJoinRequest.objects.filter(faculty=user, status=WalletJoinRequestStatus.APPROVED)
        .select_related("student", "student__department")
        .order_by("student__name")
    )
    seen: set[int] = set()
    linked = []
    for jr in students:
        if jr.student_id in seen:
            continue
        seen.add(jr.student_id)
        linked.append(
            {
                "id": jr.student_id,
                "name": _display_name(jr.student),
                "email": jr.student.email or "",
                "enrollment": jr.student.emp_id or "",
                "user_type_label": _type_label(jr.student.user_type),
                "department_name": jr.student.department.name if jr.student.department_id else "",
                "linked_at": (jr.responded_at or jr.created_at).isoformat() if (jr.responded_at or jr.created_at) else None,
            }
        )
    data.update(
        {
            "designation": getattr(user, "designation", "") or "",
            "phone": " · ".join(phones),
            "wallet_created_at": wallet.created_at.isoformat() if wallet.created_at else None,
            "total_credits": _money(sum((Decimal(i["total_credits"]) for i in data["sub_wallets"]), ZERO)),
            "total_debits": _money(sum((Decimal(i["total_debits"]) for i in data["sub_wallets"]), ZERO)),
            "students": linked,
            "credit_departments": [
                {"value": str(d.pk), "label": d.name}
                for d in Department.objects.filter(department_type=DepartmentType.INTERNAL)
                .exclude(pk__in=[s.department_id for s in subs])
                .order_by("name")
            ],
        }
    )
    return data


# --- Transactions -------------------------------------------------------------------------------------------


TXN_ORDERING = {
    "created_at": ("created_at", "id"),
    "amount": ("amount", "id"),
    "owner": ("sub_wallet__wallet__user__name", "id"),
}


def filter_transactions(qs, params):
    owner = _int(params.get("owner"))
    if owner:
        qs = qs.filter(sub_wallet__wallet__user_id=owner)
    dept = _int(params.get("owner_department"))
    if dept:
        qs = qs.filter(sub_wallet__wallet__user__department_id=dept)
    types = _csv(params, "owner_type")
    if types:
        qs = qs.filter(sub_wallet__wallet__user__user_type__in=types)
    sw = _int(params.get("sub_wallet"))
    if sw:
        qs = qs.filter(sub_wallet_id=sw)
    sw_dept = _int(params.get("sub_wallet_department"))
    if sw_dept:
        qs = qs.filter(sub_wallet__department_id=sw_dept)
    related = _int(params.get("related_user"))
    if related:
        qs = qs.filter(related_user_id=related)
    ttype = (params.get("type") or "").strip().lower()
    if ttype in ("credit", "debit"):
        qs = qs.filter(transaction_type=ttype)
    cats = _csv(params, "category")
    if cats:
        qs = qs.filter(category__in=cats)
    performers = _csv(params, "performer")
    if performers:
        qs = qs.filter(performer__in=performers)
    d_from, d_to = _date(params.get("date_from")), _date(params.get("date_to"))
    if d_from:
        qs = qs.filter(created_at__date__gte=d_from)
    if d_to:
        qs = qs.filter(created_at__date__lte=d_to)
    lo, hi = _decimal(params.get("amount_min")), _decimal(params.get("amount_max"))
    if lo is not None:
        qs = qs.filter(amount__gte=lo)
    if hi is not None:
        qs = qs.filter(amount__lte=hi)
    booking = (params.get("booking") or "").strip()
    if booking:
        qs = qs.filter(description__icontains=booking)
    search = (params.get("search") or "").strip()
    if search:
        cond = (
            Q(description__icontains=search)
            | Q(admin_adjustment__reference__iexact=search)
            | Q(admin_adjustment__remarks__icontains=search)
            | Q(admin_adjustment__external_reference__icontains=search)
            | Q(direct_recharge__reference__iexact=search)
        )
        digits = re.sub(r"^(?:txn[-\s#]*|#)", "", search, flags=re.I)
        if digits.isdigit():
            cond |= Q(pk=int(digits))
        if not owner:
            cond |= Q(sub_wallet__wallet__user__name__icontains=search) | Q(sub_wallet__wallet__user__email__icontains=search)
        qs = qs.filter(cond)
    return qs


def transactions_queryset():
    return annotate_source(
        SubWalletTransaction.objects.select_related(
            "sub_wallet",
            "sub_wallet__department",
            "sub_wallet__wallet",
            "sub_wallet__wallet__user",
            "sub_wallet__wallet__user__department",
            "related_user",
        )
    )


def _balance_after_map(rows: list[SubWalletTransaction]) -> dict[int, Decimal]:
    """Balance right after each row: the sub-wallet's current balance minus every later transaction."""
    by_sw: dict[int, list[SubWalletTransaction]] = defaultdict(list)
    for t in rows:
        by_sw[t.sub_wallet_id].append(t)
    out: dict[int, Decimal] = {}
    for sw_id, txns in by_sw.items():
        current = Decimal(txns[0].sub_wallet.balance)
        later = SubWalletTransaction.objects.filter(sub_wallet_id=sw_id, id__gt=min(t.id for t in txns)).order_by("-id")
        later_rows = list(later.values_list("id", "transaction_type", "amount"))
        acc = ZERO
        j = 0
        for t in sorted(txns, key=lambda x: -x.id):
            while j < len(later_rows) and later_rows[j][0] > t.id:
                tid, ttype, amt = later_rows[j]
                acc += amt if ttype == SubWalletTransaction.TransactionType.CREDIT else -amt
                j += 1
            out[t.id] = current - acc
    return out


def _description_display(txn: SubWalletTransaction) -> str:
    from iic_booking.users.serializers.wallet_serializer import SubWalletTransactionSerializer

    try:
        return SubWalletTransactionSerializer().get_description_display(txn)
    except Exception:  # noqa: BLE001
        return txn.description or ""


def serialize_transactions(rows: list[SubWalletTransaction], *, start: int = 0) -> list[dict[str, Any]]:
    from iic_booking.equipment.models import Booking

    labels = {key: label for key, label, _ in category_rules()}
    balances = _balance_after_map(rows)
    ids = [t.pk for t in rows]
    adjustments = {
        a.sub_wallet_transaction_id: a
        for a in WalletAdminAdjustment.objects.filter(sub_wallet_transaction_id__in=ids).select_related("performed_by")
    }
    from iic_booking.users.models.wallet_payment_modes import WalletDirectRecharge

    recharges = {
        r.sub_wallet_transaction_id: r
        for r in WalletDirectRecharge.objects.filter(sub_wallet_transaction_id__in=ids).select_related("performed_by")
    }
    refs = {t.pk: booking_reference(t.description) for t in rows}
    numeric = {pk for code, pk in refs.values() if pk}
    booking_codes = {}
    if numeric:
        for b in Booking.objects.filter(pk__in=numeric).select_related("equipment"):
            from iic_booking.communication.utils import booking_display_id_for_email

            booking_codes[b.pk] = booking_display_id_for_email(b)
    out = []
    for i, t in enumerate(rows):
        owner = t.sub_wallet.wallet.user
        adj = adjustments.get(t.pk)
        dwr = recharges.get(t.pk)
        performer = getattr(t, "performer", "system")
        if adj:
            performed_by = _display_name(adj.performed_by)
        elif dwr:
            performed_by = _display_name(dwr.performed_by)
        elif performer == "admin":
            performed_by = "Administrator"
        elif performer == "user":
            performed_by = _display_name(t.related_user) or _display_name(owner)
        else:
            performed_by = "System"
        code, booking_pk = refs[t.pk]
        if booking_pk and not code:
            code = booking_codes.get(booking_pk)
        if adj:
            reason_label = WalletAdminAdjustmentReason(adj.reason).label if adj.reason in WalletAdminAdjustmentReason.values else adj.reason
            remarks = f"{reason_label}: {adj.remarks}"
            reference = adj.reference
            external = adj.external_reference
        elif dwr:
            remarks = dwr.remarks
            reference = dwr.reference
            external = dwr.reference_number
        else:
            remarks, reference, external = "", "", ""
        balance_after = balances.get(t.pk)
        out.append(
            {
                "s_no": start + i + 1,
                "id": t.pk,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "transaction_type": t.transaction_type,
                "amount": _money(t.amount),
                "category": getattr(t, "category", "other"),
                "category_label": labels.get(getattr(t, "category", "other"), "Other"),
                "performer": performer,
                "performed_by": performed_by,
                "booking_code": code or "",
                "booking_pk": booking_pk or None,
                "sub_wallet_id": t.sub_wallet_id,
                "department_name": t.sub_wallet.department.name,
                "department_code": t.sub_wallet.department.code or "",
                "owner_id": owner.pk,
                "owner_name": _display_name(owner),
                "owner_department": owner.department.name if owner.department_id else "",
                "balance_after": _money(balance_after) if balance_after is not None else None,
                "description": _description_display(t),
                "remarks": remarks,
                "reference": reference,
                "external_reference": external,
                "related_user_name": _display_name(t.related_user) if t.related_user_id else "",
            }
        )
    return out


def list_transactions(params) -> dict[str, Any]:
    qs = filter_transactions(transactions_queryset(), params)
    page, size = _page(params)
    agg = qs.aggregate(
        count=Count("pk"),
        credits=Coalesce(Sum("amount", filter=Q(transaction_type="credit")), Value(ZERO, output_field=_MONEY)),
        debits=Coalesce(Sum("amount", filter=Q(transaction_type="debit")), Value(ZERO, output_field=_MONEY)),
    )
    key = (params.get("ordering") or "-created_at").strip()
    desc = key.startswith("-")
    fields = TXN_ORDERING.get(key.lstrip("-"), TXN_ORDERING["created_at"])
    order = [f"-{f}" if desc else f for f in fields]
    start = (page - 1) * size
    rows = list(qs.order_by(*order)[start : start + size])
    return {
        "count": agg["count"],
        "page": page,
        "page_size": size,
        "summary": {
            "transactions": agg["count"],
            "total_credits": _money(agg["credits"]),
            "total_debits": _money(agg["debits"]),
            "net": _money(agg["credits"] - agg["debits"]),
        },
        "categories": category_options(),
        "performers": PERFORMER_OPTIONS,
        "results": serialize_transactions(rows, start=start),
    }


# --- Manual credit / debit ----------------------------------------------------------------------------------


def _amount(value) -> Decimal:
    raw = str(value if value is not None else "").replace(",", "").strip()
    if not re.fullmatch(r"\d+(\.\d{1,2})?", raw):
        raise LedgerError("INVALID_AMOUNT", "Enter an amount greater than zero with at most two decimal places.")
    amount = Decimal(raw).quantize(Decimal("0.01"))
    if amount <= 0:
        raise LedgerError("INVALID_AMOUNT", "Amount must be more than zero.")
    if amount > MAX_ADJUSTMENT_AMOUNT:
        raise LedgerError("INVALID_AMOUNT", f"Amount cannot exceed ₹{MAX_ADJUSTMENT_AMOUNT:,.2f}.")
    return amount


def _direction(value) -> str:
    d = str(value or "").strip().lower()
    if d not in WalletAdminAdjustmentDirection.values:
        raise LedgerError("INVALID_DIRECTION", "Choose credit or debit.")
    return d


def _resolve_target(data: dict[str, Any], direction: str):
    """(owner, wallet, sub_wallet or None, department). A credit may open a new department sub-wallet."""
    wallet = Wallet.objects.select_related("user").filter(user_id=_int(data.get("owner_id"))).first()
    if wallet is None:
        raise LedgerError("WALLET_NOT_FOUND", "This user has no wallet.", status=404)
    sw_id = _int(data.get("sub_wallet_id"))
    if sw_id:
        sub = SubWallet.objects.select_related("department").filter(pk=sw_id, wallet=wallet).first()
        if sub is None:
            raise LedgerError("SUB_WALLET_NOT_FOUND", "Select one of this owner's sub-wallets.", status=404)
        return wallet.user, wallet, sub, sub.department
    department = Department.objects.filter(
        pk=_int(data.get("department_id")), department_type=DepartmentType.INTERNAL
    ).first()
    if department is None:
        raise LedgerError("SUB_WALLET_REQUIRED", "Select the sub-wallet.")
    sub = SubWallet.objects.select_related("department").filter(wallet=wallet, department=department).first()
    if sub is None and direction == WalletAdminAdjustmentDirection.DEBIT:
        raise LedgerError("SUB_WALLET_NOT_FOUND", "There is no sub-wallet for this department to debit.", status=404)
    return wallet.user, wallet, sub, department


def _check_debit(balance: Decimal, amount: Decimal) -> None:
    if balance - amount < ZERO:
        raise LedgerError(
            "INSUFFICIENT_BALANCE",
            f"A debit cannot take the balance below ₹0.00. Available: ₹{max(balance, ZERO):,.2f}.",
        )


def preview_adjustment(data: dict[str, Any]) -> dict[str, Any]:
    direction = _direction(data.get("direction"))
    amount = _amount(data.get("amount"))
    owner, wallet, sub, department = _resolve_target(data, direction)
    before = Decimal(sub.balance) if sub else ZERO
    if direction == WalletAdminAdjustmentDirection.DEBIT:
        _check_debit(before, amount)
    after = before + amount if direction == WalletAdminAdjustmentDirection.CREDIT else before - amount
    return {
        "owner": {"id": owner.pk, "name": _display_name(owner), "email": owner.email or ""},
        "direction": direction,
        "sub_wallet_id": sub.pk if sub else None,
        "sub_wallet_exists": sub is not None,
        "department": {"id": department.pk, "name": department.name, "code": department.code or ""},
        "amount": _money(amount),
        "balance_before": _money(before),
        "balance_after": _money(after),
    }


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    reason = str(data.get("reason") or "").strip()
    if reason not in WalletAdminAdjustmentReason.values:
        raise LedgerError("REASON_REQUIRED", "Select the reason.")
    remarks = str(data.get("remarks") or "").strip()
    if len(remarks) < 3:
        raise LedgerError("REMARKS_REQUIRED", "Remarks are required (at least 3 characters).")
    notify = data.get("notify_owner", True)
    if isinstance(notify, str):
        notify = notify.strip().lower() not in ("0", "false", "no", "off", "")
    return {
        "direction": _direction(data.get("direction")),
        "amount": _amount(data.get("amount")),
        "reason": reason,
        "remarks": remarks[:2000],
        "external_reference": str(data.get("external_reference") or "").strip()[:120],
        "notify_owner": bool(notify),
    }


def _description(record: WalletAdminAdjustment) -> str:
    verb = "credit" if record.direction == WalletAdminAdjustmentDirection.CREDIT else "debit"
    text = f"Manual admin {verb} {record.reference} — {record.get_reason_display()}: {record.remarks[:200]}"
    if record.external_reference:
        text += f" (Reference no. {record.external_reference})"
    return text


def perform_adjustment(*, actor, data: dict[str, Any], ip: str | None = None, user_agent: str = ""):
    """Credit or debit a sub-wallet once per ``client_request_id``. Returns ``(record, created)``."""
    from iic_booking.equipment.dept_admin_actions import record_staff_action

    client_request_id = str(data.get("client_request_id") or "").strip()
    if not CLIENT_REQUEST_ID_RE.fullmatch(client_request_id):
        raise LedgerError("CLIENT_REQUEST_ID_REQUIRED", "A client request id (8–64 characters) is required.")
    clean = _validate(data)

    def replay(existing: WalletAdminAdjustment):
        same = (
            existing.performed_by_id == actor.pk
            and existing.amount == clean["amount"]
            and existing.direction == clean["direction"]
            and existing.wallet.user_id == _int(data.get("owner_id"))
        )
        if not same:
            raise LedgerError("DUPLICATE_REQUEST_ID", "This request id was already used for a different entry.", status=409)
        return existing, False

    existing = WalletAdminAdjustment.objects.select_related("wallet").filter(client_request_id=client_request_id).first()
    if existing:
        return replay(existing)

    owner, wallet, sub, department = _resolve_target(data, clean["direction"])
    try:
        with transaction.atomic():
            if sub is None:
                sub, _ = SubWallet.objects.get_or_create(wallet=wallet, department=department, defaults={"balance": ZERO})
            sub = SubWallet.objects.select_for_update().select_related("department").get(pk=sub.pk)
            before = Decimal(sub.balance)
            if clean["direction"] == WalletAdminAdjustmentDirection.CREDIT:
                txn = sub.credit(clean["amount"], "Manual admin credit", related_user=owner)
            else:
                _check_debit(before, clean["amount"])
                try:
                    txn = sub.debit(clean["amount"], "Manual admin debit", related_user=owner)
                except ValueError as exc:
                    raise LedgerError("INSUFFICIENT_BALANCE", str(exc)) from exc
            sub.refresh_from_db(fields=["balance"])
            record = WalletAdminAdjustment.objects.create(
                client_request_id=client_request_id,
                direction=clean["direction"],
                wallet=wallet,
                sub_wallet=sub,
                amount=clean["amount"],
                reason=clean["reason"],
                remarks=clean["remarks"],
                external_reference=clean["external_reference"],
                balance_before=before,
                balance_after=Decimal(sub.balance),
                sub_wallet_transaction=txn,
                performed_by=actor,
                notify_owner=clean["notify_owner"],
                ip_address=ip or None,
                user_agent=(user_agent or "")[:255],
            )
            prefix = "WAC" if clean["direction"] == WalletAdminAdjustmentDirection.CREDIT else "WAD"
            record.reference = f"{prefix}-{timezone.now().year}-{record.pk:06d}"
            record.save(update_fields=["reference"])
            SubWalletTransaction.objects.filter(pk=txn.pk).update(description=_description(record))
            record_staff_action(
                actor,
                f"wallet_admin_{clean['direction']}",
                reference=record.reference,
                wallet_owner_id=owner.pk,
                sub_wallet_id=sub.pk,
                department_id=department.pk,
                amount=str(record.amount),
                reason=record.reason,
                balance_before=str(before),
                balance_after=str(record.balance_after),
                notify_owner=record.notify_owner,
            )
            if record.notify_owner:
                record_id = record.pk
                transaction.on_commit(lambda: notify_adjustment(record_id))
    except IntegrityError:
        existing = WalletAdminAdjustment.objects.select_related("wallet").filter(client_request_id=client_request_id).first()
        if existing is None:
            raise
        return replay(existing)
    return record, True


def serialize_adjustment(record: WalletAdminAdjustment) -> dict[str, Any]:
    return {
        "id": record.pk,
        "reference": record.reference,
        "direction": record.direction,
        "amount": _money(record.amount),
        "reason": record.reason,
        "reason_label": record.get_reason_display(),
        "remarks": record.remarks,
        "external_reference": record.external_reference,
        "balance_before": _money(record.balance_before),
        "balance_after": _money(record.balance_after),
        "sub_wallet_id": record.sub_wallet_id,
        "department_name": record.sub_wallet.department.name,
        "transaction_id": record.sub_wallet_transaction_id,
        "owner_id": record.wallet.user_id,
        "notify_owner": record.notify_owner,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


def notify_adjustment(record_id: int) -> None:
    """Email (code-default template) and in-app notice to the wallet owner."""
    from html import escape

    from iic_booking.communication.email_branding import format_email_datetime, format_inr, wrap_email_html
    from iic_booking.communication.styled_transactional_emails import _send
    from iic_booking.communication.utils import get_frontend_absolute_url

    try:
        r = WalletAdminAdjustment.objects.select_related("wallet__user", "sub_wallet__department").get(pk=record_id)
    except WalletAdminAdjustment.DoesNotExist:
        return
    owner = r.wallet.user
    credit = r.direction == WalletAdminAdjustmentDirection.CREDIT
    amount = format_inr(r.amount) or f"₹{r.amount:,.2f}"
    dept = r.sub_wallet.department.name
    headline = f"{amount} {'credited to' if credit else 'debited from'} your {dept} wallet"
    rows = [
        ("Reference", r.reference),
        ("Type", "Credit" if credit else "Debit"),
        ("Amount", amount),
        ("Department sub-wallet", dept),
        ("Reason", r.get_reason_display()),
        ("Remarks", r.remarks),
        ("Reference no.", r.external_reference or "—"),
        ("Date", format_email_datetime(r.created_at)),
        ("New balance", format_inr(r.balance_after) or f"₹{r.balance_after:,.2f}"),
    ]
    link = get_frontend_absolute_url("/wallet")
    intro = (
        f"The IIC Main Administrator has {'credited' if credit else 'debited'} your wallet. "
        "The entry appears in your wallet transactions."
    )
    text = f"Dear {_display_name(owner)},\n\n{intro}\n\n" + "\n".join(f"{k}: {v}" for k, v in rows) + f"\n\nView your wallet: {link}\n"
    table = "".join(
        f"<tr><td style='padding:6px 12px 6px 0;color:#475569;'><strong>{escape(k)}</strong></td>"
        f"<td style='padding:6px 0;color:#0f172a;'>{escape(str(v))}</td></tr>"
        for k, v in rows
    )
    body = (
        f"<p>Dear {escape(_display_name(owner))},</p><p>{escape(intro)}</p>"
        f"<table cellpadding='0' cellspacing='0' style='font-family:Arial,Helvetica,sans-serif;font-size:14px;'>{table}</table>"
        f"<p style='margin-top:18px;'><a href='{escape(link)}'>View your wallet</a></p>"
    )
    subject = f"[{r.reference}] {headline}"
    try:
        if owner.email:
            _send(owner.email, subject, text, wrap_email_html(title=headline, subtitle=r.reference, body_inner_html=body))
            WalletAdminAdjustment.objects.filter(pk=r.pk).update(email_sent_at=timezone.now())
    except Exception:  # noqa: BLE001
        logger.exception("wallet adjustment email failed for adjustment %s", r.pk)
    try:
        from iic_booking.communication.in_app import notify_in_app

        notify_in_app(
            [owner],
            title="Wallet credited" if credit else "Wallet debited",
            message=f"{headline} ({r.reference}).",
            link="/wallet",
            notification_type="success" if credit else "warning",
            event="wallet.admin_adjustment",
            extra={"wallet_admin_adjustment_id": r.pk},
        )
    except Exception:  # noqa: BLE001
        logger.debug("wallet adjustment in-app notification skipped", exc_info=True)
