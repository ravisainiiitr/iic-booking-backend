"""Wallet ledger (Main Administrator): students linked to a wallet owner.

A student is linked through ``WalletJoinRequest`` — the relation used for booking against a faculty wallet, the
linked-students count and the supervisor spending limits. Each student appears once, with their approved link if
they have one, otherwise their most recent request. Faculty supervision recorded on the user profile
(``User.supervisor``, for Post Doctoral Fellows / Research Associates) does not let anyone book against the wallet,
so those users are returned separately as ``supervised``.

Spend is taken from this owner's sub-wallet transactions that name the student as the booking user: booking, extra
and training charges minus refunds. Every aggregate runs as one grouped query, so the number of queries does not
grow with the number of students.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Max, Q, Sum
from django.utils import timezone

from iic_booking.users.admin_wallet_ledger import (
    LedgerError,
    ZERO,
    _date,
    _display_name,
    _int,
    _money,
    _type_label,
    annotate_source,
)
from iic_booking.users.models import User
from iic_booking.users.models.wallet import SubWalletTransaction, Wallet, WalletJoinRequest, WalletJoinRequestStatus

CHARGE_CATEGORIES = ("booking_charge", "extra_charge", "training_charge")

STATUS_OPTIONS = [
    {"value": "linked", "label": "Linked"},
    {"value": "pending", "label": "Pending approval"},
    {"value": "declined", "label": "Declined"},
    {"value": "removed", "label": "Removed by owner"},
    {"value": "cancelled", "label": "Withdrawn by student"},
]
_STATUS_LABELS = {o["value"]: o["label"] for o in STATUS_OPTIONS}
_STATUS_RANK = {"linked": 0, "pending": 1, "removed": 2, "declined": 3, "cancelled": 4}

ORDERINGS = ("name", "status", "linked_at", "total_spent", "range_spent", "last_booking", "department")


def link_status(link: WalletJoinRequest) -> str:
    if link.status == WalletJoinRequestStatus.APPROVED:
        return "linked"
    if link.status == WalletJoinRequestStatus.PENDING:
        return "pending"
    if link.status == WalletJoinRequestStatus.REJECTED:
        return "declined"
    return "removed" if link.responded_at else "cancelled"


def _current_links(wallet: Wallet) -> list[WalletJoinRequest]:
    """One request per student: the approved one if any, else the latest."""
    rows = (
        WalletJoinRequest.objects.filter(Q(wallet_id=wallet.pk) | Q(wallet__isnull=True, faculty_id=wallet.user_id))
        .exclude(student_id=wallet.user_id)
        .select_related("student", "student__department")
        .order_by("-id")
    )
    chosen: dict[int, WalletJoinRequest] = {}
    for link in rows:
        current = chosen.get(link.student_id)
        if current is None or (
            link.status == WalletJoinRequestStatus.APPROVED and current.status != WalletJoinRequestStatus.APPROVED
        ):
            chosen[link.student_id] = link
    return list(chosen.values())


def _limit_usage(links: list[WalletJoinRequest], now: datetime) -> dict[int, tuple[Decimal, Decimal]]:
    """(week spend, month spend) per approved link, counted as the spending-limit rule counts it."""
    from iic_booking.equipment.models import Booking
    from iic_booking.users.student_spending_limits import _not_charged_statuses, month_bounds, week_bounds

    approved = [l for l in links if l.status == WalletJoinRequestStatus.APPROVED]
    if not approved:
        return {}
    w_start, w_end = week_bounds(now)
    m_start, m_end = month_bounds(now)
    lo, hi = min(w_start, m_start), max(w_end, m_end)
    bookings: dict[int, list[tuple[datetime, Decimal]]] = defaultdict(list)
    for user_id, created, charge, pending in (
        Booking.objects.filter(user_id__in=[l.student_id for l in approved], created_at__gte=lo, created_at__lt=hi)
        .exclude(status__in=_not_charged_statuses())
        .values_list("user_id", "created_at", "total_charge", "charge_recalculation_pending_amount")
    ):
        unpaid = Decimal(pending or 0) if (pending or 0) > 0 else ZERO
        bookings[user_id].append((created, Decimal(charge or 0) - unpaid))

    def spend(link, start, end) -> Decimal:
        since = max(start, link.responded_at) if link.responded_at else start
        total = sum((amt for at, amt in bookings.get(link.student_id, []) if since <= at < end), ZERO)
        return total if total > ZERO else ZERO

    return {l.pk: (spend(l, w_start, w_end), spend(l, m_start, m_end)) for l in approved}


def _wallet_spend(wallet: Wallet, student_ids: list[int], d_from, d_to) -> dict[int, dict[str, Any]]:
    if not student_ids:
        return {}
    qs = annotate_source(
        SubWalletTransaction.objects.filter(sub_wallet__wallet=wallet, related_user_id__in=student_ids)
    )
    charge = Q(transaction_type="debit", category__in=CHARGE_CATEGORIES)
    refund = Q(transaction_type="credit", category="refund")
    in_range = Q()
    if d_from:
        in_range &= Q(created_at__date__gte=d_from)
    if d_to:
        in_range &= Q(created_at__date__lte=d_to)
    rows = qs.order_by().values("related_user_id").annotate(
        charged=Sum("amount", filter=charge),
        refunded=Sum("amount", filter=refund),
        range_charged=Sum("amount", filter=charge & in_range),
        range_refunded=Sum("amount", filter=refund & in_range),
        last_charge=Max("created_at", filter=charge),
    )
    out: dict[int, dict[str, Any]] = {}
    for r in rows:
        charged, refunded = r["charged"] or ZERO, r["refunded"] or ZERO
        r_charged, r_refunded = r["range_charged"] or ZERO, r["range_refunded"] or ZERO
        out[r["related_user_id"]] = {
            "charged": charged,
            "refunded": refunded,
            "spent": max(charged - refunded, ZERO),
            "range_spent": max(r_charged - r_refunded, ZERO),
            "last_charge": r["last_charge"],
        }
    subs: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for user_id, sw_id, name in (
        qs.filter(charge)
        .order_by("sub_wallet__department__name")
        .values_list("related_user_id", "sub_wallet_id", "sub_wallet__department__name")
        .distinct()
    ):
        subs[user_id].append({"id": sw_id, "department_name": name})
    for user_id, items in subs.items():
        out.setdefault(user_id, {"charged": ZERO, "refunded": ZERO, "spent": ZERO, "range_spent": ZERO, "last_charge": None})
        out[user_id]["sub_wallets"] = items
    return out


def _last_bookings(student_ids: list[int]) -> dict[int, datetime]:
    from iic_booking.equipment.models import Booking

    if not student_ids:
        return {}
    return dict(
        Booking.objects.filter(user_id__in=student_ids)
        .order_by()
        .values("user_id")
        .annotate(last=Max("created_at"))
        .values_list("user_id", "last")
    )


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _matches(search: str, *values: str) -> bool:
    needle = search.lower()
    return any(needle in (v or "").lower() for v in values)


def _sort(rows: list[dict[str, Any]], ordering: str) -> list[dict[str, Any]]:
    key = (ordering or "status").strip()
    desc = key.startswith("-")
    key = key.lstrip("-")
    if key not in ORDERINGS:
        key, desc = "status", False
    name = lambda r: (r["name"] or "").lower()  # noqa: E731
    if key == "name":
        rows.sort(key=name, reverse=desc)
    elif key == "department":
        rows.sort(key=lambda r: ((r["department_name"] or "").lower(), name(r)), reverse=desc)
    elif key == "status":
        rows.sort(key=lambda r: (_STATUS_RANK.get(r["status"], 9), name(r)), reverse=desc)
    else:
        field = {
            "linked_at": "responded_at",
            "total_spent": "total_spent",
            "range_spent": "range_spent",
            "last_booking": "last_booking_at",
        }[key]
        present = [r for r in rows if r.get(field) not in (None, "")]
        missing = [r for r in rows if r.get(field) in (None, "")]
        numeric = field in ("total_spent", "range_spent")
        present.sort(key=lambda r: Decimal(r[field]) if numeric else r[field], reverse=desc)
        missing.sort(key=name)
        rows[:] = present + missing
    return rows


def linked_students(params) -> dict[str, Any]:
    owner_id = _int(params.get("owner"))
    wallet = Wallet.objects.select_related("user").filter(user_id=owner_id).first() if owner_id else None
    if wallet is None:
        raise LedgerError("WALLET_NOT_FOUND", "This user has no wallet.", status=404)
    d_from, d_to = _date(params.get("date_from")), _date(params.get("date_to"))
    now = timezone.now()
    links = _current_links(wallet)
    ids = [l.student_id for l in links]
    usage = _limit_usage(links, now)
    spend = _wallet_spend(wallet, ids, d_from, d_to)
    last_booking = _last_bookings(ids)

    rows: list[dict[str, Any]] = []
    for link in links:
        st = link.student
        status = link_status(link)
        s = spend.get(st.pk, {})
        week, month = usage.get(link.pk, (None, None))
        rows.append(
            {
                "join_request_id": link.pk,
                "student_id": st.pk,
                "name": _display_name(st),
                "enrollment": st.emp_id or "",
                "email": st.email or "",
                "department_name": st.department.name if st.department_id else "",
                "user_type": st.user_type,
                "user_type_label": _type_label(st.user_type),
                "is_active": bool(st.is_active),
                "status": status,
                "status_label": _STATUS_LABELS[status],
                "requested_at": _iso(link.created_at),
                "responded_at": _iso(link.responded_at),
                "sub_wallets": s.get("sub_wallets", []),
                "spending_limit_enabled": bool(link.spending_limit_enabled) and status == "linked",
                "weekly_limit": _money(link.weekly_limit_inr) if link.weekly_limit_inr is not None else None,
                "monthly_limit": _money(link.monthly_limit_inr) if link.monthly_limit_inr is not None else None,
                "week_spent": _money(week) if week is not None else None,
                "month_spent": _money(month) if month is not None else None,
                "total_charged": _money(s.get("charged", ZERO)),
                "total_refunded": _money(s.get("refunded", ZERO)),
                "total_spent": _money(s.get("spent", ZERO)),
                "range_spent": _money(s.get("range_spent", ZERO)),
                "last_charge_at": _iso(s.get("last_charge")),
                "last_booking_at": _iso(last_booking.get(st.pk)),
            }
        )

    counts = defaultdict(int)
    for r in rows:
        counts[r["status"]] += 1
    search = (params.get("search") or "").strip()
    if search:
        rows = [r for r in rows if _matches(search, r["name"], r["email"], r["enrollment"], r["department_name"])]
    statuses = [s for s in (params.get("status") or "").split(",") if s.strip()]
    if statuses:
        rows = [r for r in rows if r["status"] in statuses]
    _sort(rows, params.get("ordering") or "status")
    for i, r in enumerate(rows, start=1):
        r["s_no"] = i

    linked_ids = {l.student_id for l in links if l.status == WalletJoinRequestStatus.APPROVED}
    supervised = [
        {
            "student_id": u.pk,
            "name": _display_name(u),
            "enrollment": u.emp_id or "",
            "email": u.email or "",
            "department_name": u.department.name if u.department_id else "",
            "user_type_label": _type_label(u.user_type),
            "is_active": bool(u.is_active),
        }
        for u in User.objects.filter(supervisor_id=wallet.user_id)
        .exclude(pk__in=linked_ids)
        .select_related("department")
        .order_by("name", "email")
    ]
    if search:
        supervised = [r for r in supervised if _matches(search, r["name"], r["email"], r["enrollment"], r["department_name"])]
    for i, r in enumerate(supervised, start=1):
        r["s_no"] = i

    from iic_booking.users.student_spending_limits import month_bounds, week_bounds

    w_start, w_end = week_bounds(now)
    m_start, _ = month_bounds(now)
    return {
        "owner_id": wallet.user_id,
        "owner_name": _display_name(wallet.user),
        "count": len(rows),
        "summary": {
            "linked": counts["linked"],
            "pending": counts["pending"],
            "removed": counts["removed"],
            "declined": counts["declined"],
            "cancelled": counts["cancelled"],
            "with_limits": sum(1 for r in rows if r["spending_limit_enabled"]),
            "total_spent": _money(sum((Decimal(r["total_spent"]) for r in rows), ZERO)),
            "range_spent": _money(sum((Decimal(r["range_spent"]) for r in rows), ZERO)),
            "supervised": len(supervised),
        },
        "period": {
            "week_start": w_start.date().isoformat(),
            "week_end": (w_end - timedelta(days=1)).date().isoformat(),
            "month_start": m_start.date().isoformat(),
        },
        "statuses": STATUS_OPTIONS,
        "results": rows,
        "supervised": supervised,
    }
