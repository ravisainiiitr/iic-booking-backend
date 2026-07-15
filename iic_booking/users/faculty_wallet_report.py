"""Aggregated expense / recharge insight for IITR Faculty shared wallets (supervisor + linked students)."""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from django.db.models import Count, Q, Sum
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.users.models import User, Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    SubWalletTransaction,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)


# Bookings that should not count toward "spend" (refunded / not confirmed).
_EXCLUDED_EXPENSE_STATUSES = frozenset(
    {
        BookingStatus.REFUNDED,
        BookingStatus.CANCELLED,
        BookingStatus.PENDING,
        BookingStatus.WAITLISTED,
    }
)


def _parse_report_dates(
    date_from: Optional[str],
    date_to: Optional[str],
) -> tuple[Any, Any]:
    today = timezone.localdate()
    if date_from:
        try:
            start = datetime.strptime(date_from, "%Y-%m-%d").date()
        except ValueError:
            start = today.replace(day=1)
    else:
        start = today.replace(day=1)
    if date_to:
        try:
            end = datetime.strptime(date_to, "%Y-%m-%d").date()
        except ValueError:
            end = today
    else:
        end = start.replace(day=monthrange(start.year, start.month)[1])
    if end < start:
        start, end = end, start
    return start, end


def _classify_credit(amount: Decimal, description: str | None) -> str:
    """Return 'refund', 'internal_transfer', 'withdrawal_reversal', or 'recharge_or_other'."""
    desc = (description or "").strip()
    dl = desc.lower()
    if desc.startswith("Refund") or dl.startswith("refund"):
        return "refund"
    if desc.startswith("Transfer from"):
        return "internal_transfer"
    if "wallet withdrawal request" in dl and "reversal" in dl:
        return "withdrawal_reversal"
    return "recharge_or_other"


def _approved_recharge_rows_for_wallet(wallet: Wallet, start, end) -> list[dict[str, Any]]:
    """Approved accounts-team recharge requests credited in the date range (approval / response date)."""
    qs = (
        WalletRechargeRequest.objects.filter(
            wallet=wallet,
            status=WalletRechargeRequestStatus.APPROVED,
        )
        .filter(
            Q(responded_at__date__gte=start, responded_at__date__lte=end)
            | Q(responded_at__isnull=True, updated_at__date__gte=start, updated_at__date__lte=end)
        )
        .select_related("department", "project", "project__faculty", "user")
        .order_by("-responded_at", "-updated_at", "-id")
    )
    out: list[dict[str, Any]] = []
    for r in qs:
        proj = r.project
        pi = getattr(proj, "faculty", None) if proj else None
        pi_name = ""
        pi_email = ""
        if pi:
            pi_name = (getattr(pi, "name", None) or "").strip() or (getattr(pi, "email", None) or "")
            pi_email = getattr(pi, "email", None) or ""
        out.append(
            {
                "id": r.id,
                "amount": str(r.amount),
                "department_name": getattr(r.department, "name", None) or "—",
                "project_name": getattr(proj, "name", None) or "",
                "project_code": getattr(proj, "project_code", None) or "",
                "project_agency": getattr(proj, "agency", None) or "",
                "project_details_legacy": (r.project_details or "").strip(),
                "project_head_name": pi_name,
                "project_head_email": pi_email,
                "requested_by_name": (getattr(r.user, "name", None) or "").strip() or (getattr(r.user, "email", None) or ""),
                "requested_by_email": getattr(r.user, "email", None) or "",
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "responded_at": r.responded_at.isoformat() if r.responded_at else None,
                "approved_by_email": (r.approved_by_email or "").strip(),
                "response_message": (r.response_message or "").strip(),
            }
        )
    return out


def build_faculty_wallet_expense_report(
    faculty_user: User,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    equipment_id: Optional[int] = None,
) -> dict[str, Any]:
    """
    Build JSON-serializable report for the faculty-owned wallet and approved linked members.

    Spend is derived from Booking rows (created in range), excluding refunded/cancelled/pending/waitlisted.
    Wallet movements summarise debits/credits in the same date range.
    """
    start, end = _parse_report_dates(date_from, date_to)

    if faculty_user.user_type != UserType.FACULTY:
        return {
            "error": "only_faculty",
            "message": "This report is only available for IITR Faculty accounts.",
        }

    wallet = Wallet.objects.filter(user=faculty_user).first()
    if not wallet:
        return {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "current_balance": "0.00",
            "sub_wallets": [],
            "linked_students": [],
            "member_user_ids": [faculty_user.pk],
            "period_wallet_movements": {
                "total_debits": "0.00",
                "total_credits": "0.00",
                "recharges_and_similar_credits": "0.00",
                "refund_credits": "0.00",
                "internal_transfer_credits": "0.00",
                "withdrawal_reversal_credits": "0.00",
            },
            "period_booking_spend": {
                "total": "0.00",
                "booking_count": 0,
            },
            "by_member": [],
            "by_equipment": [],
            "equipment_filter_id": equipment_id,
            "approved_recharges": [],
        }

    member_ids: set[int] = {faculty_user.pk}
    linked = list(
        WalletJoinRequest.objects.filter(
            wallet=wallet,
            status=WalletJoinRequestStatus.APPROVED,
        ).select_related("student")
    )
    linked_payload = []
    for jr in linked:
        st = jr.student
        if not st:
            continue
        member_ids.add(st.pk)
        linked_payload.append(
            {
                "user_id": st.pk,
                "name": (getattr(st, "name", None) or "").strip() or st.email,
                "email": st.email or "",
                "user_type": st.user_type or "",
            }
        )

    sub_wallets = list(wallet.sub_wallets.select_related("department").all())
    sub_wallet_rows = [
        {
            "department_id": sw.department_id,
            "department_name": getattr(sw.department, "name", None) or "—",
            "balance": str(sw.balance),
        }
        for sw in sub_wallets
    ]

    sw_pk_list = [sw.id for sw in sub_wallets]
    from iic_booking.users.test_accounts import exclude_test_bookings, exclude_test_wallet_txns

    tx_qs = exclude_test_wallet_txns(
        SubWalletTransaction.objects.filter(
            sub_wallet_id__in=sw_pk_list,
            created_at__date__gte=start,
            created_at__date__lte=end,
        )
    )

    total_debits = Decimal("0.00")
    total_credits = Decimal("0.00")
    recharge_sum = Decimal("0.00")
    refund_credit_sum = Decimal("0.00")
    transfer_sum = Decimal("0.00")
    reversal_sum = Decimal("0.00")

    for amt, desc, ttype in tx_qs.values_list("amount", "description", "transaction_type"):
        a = Decimal(str(amt or 0))
        if ttype == SubWalletTransaction.TransactionType.DEBIT:
            total_debits += a
        else:
            total_credits += a
            kind = _classify_credit(a, desc)
            if kind == "refund":
                refund_credit_sum += a
            elif kind == "internal_transfer":
                transfer_sum += a
            elif kind == "withdrawal_reversal":
                reversal_sum += a
            else:
                recharge_sum += a

    bq = exclude_test_bookings(
        Booking.objects.filter(
            user_id__in=member_ids,
            created_at__date__gte=start,
            created_at__date__lte=end,
        ).exclude(status__in=_EXCLUDED_EXPENSE_STATUSES)
    )

    if equipment_id is not None:
        bq = bq.filter(equipment_id=int(equipment_id))

    agg = bq.aggregate(total=Sum("total_charge"), n=Count("booking_id"))
    booking_total = Decimal(str(agg["total"] or 0))
    booking_n = int(agg["n"] or 0)

    # Per-member totals
    by_user_rows = list(
        bq.values("user_id", "user__name", "user__email", "user__user_type")
        .annotate(total_spend=Sum("total_charge"), booking_count=Count("booking_id"))
        .order_by("-total_spend")
    )

    eq_rows = list(
        bq.values(
            "equipment_id",
            "equipment__code",
            "equipment__name",
        )
        .annotate(total_spend=Sum("total_charge"), booking_count=Count("booking_id"))
        .order_by("-total_spend")
    )

    # Equipment split per user (for expandable UI)
    per_user_equipment: dict[int, list[dict[str, Any]]] = defaultdict(list)
    eq_detail_qs = (
        bq.values(
            "user_id",
            "equipment_id",
            "equipment__code",
            "equipment__name",
        )
        .annotate(total_spend=Sum("total_charge"), booking_count=Count("booking_id"))
        .order_by("user_id", "-total_spend")
    )
    for row in eq_detail_qs:
        uid = int(row["user_id"])
        per_user_equipment[uid].append(
            {
                "equipment_id": row["equipment_id"],
                "equipment_code": row["equipment__code"] or "",
                "equipment_name": row["equipment__name"] or "—",
                "total_spend": str(Decimal(str(row["total_spend"] or 0))),
                "booking_count": int(row["booking_count"] or 0),
            }
        )

    by_member: list[dict[str, Any]] = []
    for row in by_user_rows:
        uid = int(row["user_id"])
        spend = Decimal(str(row["total_spend"] or 0))
        pct = float((spend / booking_total) * 100) if booking_total > 0 else 0.0
        is_owner = uid == faculty_user.pk
        by_member.append(
            {
                "user_id": uid,
                "name": (row["user__name"] or "").strip() or (row["user__email"] or ""),
                "email": row["user__email"] or "",
                "user_type": row["user__user_type"] or "",
                "is_wallet_owner": is_owner,
                "role_label": "Supervisor (you)" if is_owner else "Linked member",
                "total_spend": str(spend),
                "booking_count": int(row["booking_count"] or 0),
                "share_of_period_spend_percent": round(pct, 2),
                "by_equipment": per_user_equipment.get(uid, []),
            }
        )

    by_equipment_out = [
        {
            "equipment_id": r["equipment_id"],
            "equipment_code": r["equipment__code"] or "",
            "equipment_name": r["equipment__name"] or "—",
            "total_spend": str(Decimal(str(r["total_spend"] or 0))),
            "booking_count": int(r["booking_count"] or 0),
        }
        for r in eq_rows
    ]

    approved_recharges = _approved_recharge_rows_for_wallet(wallet, start, end)

    return {
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "current_balance": str(wallet.total_balance),
        "sub_wallets": sub_wallet_rows,
        "linked_students": linked_payload,
        "member_user_ids": sorted(member_ids),
        "period_wallet_movements": {
            "total_debits": str(total_debits),
            "total_credits": str(total_credits),
            "recharges_and_similar_credits": str(recharge_sum),
            "refund_credits": str(refund_credit_sum),
            "internal_transfer_credits": str(transfer_sum),
            "withdrawal_reversal_credits": str(reversal_sum),
        },
        "period_booking_spend": {
            "total": str(booking_total),
            "booking_count": booking_n,
        },
        "by_member": by_member,
        "by_equipment": by_equipment_out,
        "equipment_filter_id": equipment_id,
        "approved_recharges": approved_recharges,
    }
