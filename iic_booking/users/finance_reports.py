"""Departmental finance report for Account In-charge (finance) users.

Scopes bookings, revenue, and wallet-recharge activity to the finance user's own
internal department (bookings where the equipment's home department OR the
settlement department matches), and builds the executive dashboard payload
consumed by the Finance Reports page (summary KPIs, charts, and detail tables).
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.users.models.payment import PaymentGatewayStatus, PaymentGatewayTransaction
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import (
    WalletRechargeMode,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)

# Bookings that represent recognised revenue for the finance dashboard.
_REVENUE_STATUSES = (BookingStatus.COMPLETED, BookingStatus.BOOKED)
_NON_DUE_STATUSES = (BookingStatus.CANCELLED, BookingStatus.REFUNDED)

_ORG_CATEGORY_LABELS: dict[str, str] = {code: str(label) for code, label in UserType.get_choices()}

_INSTITUTE_NAME = "Institute Instrumentation Centre"


def _dec(value: Any) -> Decimal:
    if value is None:
        return Decimal("0.00")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0.00")


def _f(value: Any) -> float:
    return round(float(_dec(value)), 2)


def _is_external_snapshot(user_type_snapshot: Optional[str]) -> bool:
    return (user_type_snapshot or "") in UserType.get_external_user_codes()


def _org_category_label(code: Optional[str]) -> str:
    code = code or "Unknown"
    return _ORG_CATEGORY_LABELS.get(code, str(code))


def _booking_hours(total_time_minutes: Optional[int]) -> float:
    return round(float(total_time_minutes or 0) / 60.0, 2)


def _department_scoped_bookings_qs(department_id: int):
    qs = Booking.objects.filter(
        Q(equipment__internal_department_id=department_id) | Q(settlement_department_id=department_id)
    )
    try:
        from iic_booking.users.test_accounts import exclude_test_bookings

        qs = exclude_test_bookings(qs)
    except ImportError:
        pass
    return qs.annotate(effective_date=Coalesce("payment_settled_at", "completed_at", "created_at"))


def _sorted_revenue_rows(bucket: dict[Any, dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for v in bucket.values():
        row = dict(v)
        row["revenue"] = _f(row["revenue"])
        rows.append(row)
    rows.sort(key=lambda r: -r["revenue"])
    return rows


def _generated_by_display(user: Any) -> str:
    name = (getattr(user, "display_name", None) or getattr(user, "name", None) or "").strip()
    email = (getattr(user, "email", None) or "").strip()
    if name and email:
        return f"{name} ({email})"
    return name or email or "—"


def _empty_report(user: Any, date_from: date, date_to: date, *, note: str = "") -> dict[str, Any]:
    department = getattr(user, "department", None)
    department_name = getattr(department, "name", "") or ""
    report_title = (
        f"Departmental Finance Report — {department_name}" if department_name else "Departmental Finance Report"
    )
    return {
        "meta": {
            "institute_name": _INSTITUTE_NAME,
            "report_title": report_title,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "generated_at": timezone.now().isoformat(),
            "generated_by": _generated_by_display(user),
            "note": note,
        },
        "summary": {
            "total_revenue": 0.0,
            "internal_revenue": 0.0,
            "external_revenue": 0.0,
            "wallet_recharges": 0.0,
            "pending_payments": 0.0,
            "refunded_amount": 0.0,
            "outstanding_dues": 0.0,
            "booking_count": 0,
            "avg_booking_value": 0.0,
        },
        "charts": {
            "revenue_trend": [],
            "internal_vs_external": [
                {"name": "Internal", "value": 0.0},
                {"name": "External", "value": 0.0},
            ],
            "revenue_by_department": [],
            "revenue_by_org_category": [],
            "equipment_revenue": [],
        },
        "tables": {
            "internal_by_department": [],
            "internal_by_equipment": [],
            "external_by_category": [],
            "equipment_analysis": [],
            "payment_analytics": {
                "wallet_recharge_approved": 0.0,
                "wallet_recharge_pending": 0.0,
                "wallet_recharge_rejected": 0.0,
                "wallet_recharge_cancelled": 0.0,
                "project_grant": 0.0,
                "direct_cash_deposit": 0.0,
                "online_payments": 0.0,
            },
        },
    }


def build_finance_report(*, user: Any, date_from: date, date_to: date) -> dict[str, Any]:
    """Build the Account In-charge (finance) executive report for [date_from, date_to] (inclusive)."""
    department_id = getattr(user, "department_id", None)
    if not department_id:
        return _empty_report(
            user,
            date_from,
            date_to,
            note="No internal department is assigned to this account.",
        )

    bookings_qs = _department_scoped_bookings_qs(department_id).filter(
        effective_date__date__gte=date_from,
        effective_date__date__lte=date_to,
    )

    revenue_rows = list(
        bookings_qs.filter(status__in=_REVENUE_STATUSES).values(
            "booking_id",
            "total_charge",
            "user_type_snapshot",
            "total_time_minutes",
            "effective_date",
            "equipment_id",
            "equipment__code",
            "equipment__name",
            "equipment__internal_department__name",
        )
    )

    total_revenue = Decimal("0.00")
    internal_revenue = Decimal("0.00")
    external_revenue = Decimal("0.00")

    trend_bucket_by_day: dict[date, Decimal] = defaultdict(Decimal)
    dept_bucket: dict[str, dict[str, Any]] = {}
    org_bucket: dict[str, dict[str, Any]] = {}
    equip_bucket: dict[int, dict[str, Any]] = {}
    equip_hours_bucket: dict[int, dict[str, Any]] = {}
    internal_dept_bucket: dict[str, dict[str, Any]] = {}
    internal_equip_bucket: dict[int, dict[str, Any]] = {}
    external_cat_bucket: dict[str, dict[str, Any]] = {}

    for row in revenue_rows:
        amt = _dec(row["total_charge"])
        total_revenue += amt
        is_ext = _is_external_snapshot(row["user_type_snapshot"])
        if is_ext:
            external_revenue += amt
        else:
            internal_revenue += amt

        eff = row["effective_date"]
        day = eff.date() if hasattr(eff, "date") else eff
        if day is not None:
            trend_bucket_by_day[day] += amt

        dept_name = row["equipment__internal_department__name"] or "Unassigned"
        dept_row = dept_bucket.setdefault(dept_name, {"name": dept_name, "revenue": Decimal("0.00"), "bookings": 0})
        dept_row["revenue"] += amt
        dept_row["bookings"] += 1

        org_label = _org_category_label(row["user_type_snapshot"])
        org_row = org_bucket.setdefault(org_label, {"name": org_label, "revenue": Decimal("0.00"), "bookings": 0})
        org_row["revenue"] += amt
        org_row["bookings"] += 1

        eid = row["equipment_id"]
        equip_row = equip_bucket.setdefault(
            eid,
            {
                "name": row["equipment__name"] or "",
                "code": row["equipment__code"] or "",
                "revenue": Decimal("0.00"),
                "bookings": 0,
            },
        )
        equip_row["revenue"] += amt
        equip_row["bookings"] += 1

        hours_row = equip_hours_bucket.setdefault(
            eid,
            {
                "name": row["equipment__name"] or "",
                "code": row["equipment__code"] or "",
                "revenue": Decimal("0.00"),
                "bookings": 0,
                "hours": 0.0,
            },
        )
        hours_row["revenue"] += amt
        hours_row["bookings"] += 1
        hours_row["hours"] += _booking_hours(row["total_time_minutes"])

        if is_ext:
            cat_row = external_cat_bucket.setdefault(
                org_label, {"name": org_label, "revenue": Decimal("0.00"), "bookings": 0}
            )
            cat_row["revenue"] += amt
            cat_row["bookings"] += 1
        else:
            int_dept_row = internal_dept_bucket.setdefault(
                dept_name, {"name": dept_name, "revenue": Decimal("0.00"), "bookings": 0}
            )
            int_dept_row["revenue"] += amt
            int_dept_row["bookings"] += 1
            int_equip_row = internal_equip_bucket.setdefault(
                eid,
                {
                    "name": row["equipment__name"] or "",
                    "code": row["equipment__code"] or "",
                    "revenue": Decimal("0.00"),
                    "bookings": 0,
                },
            )
            int_equip_row["revenue"] += amt
            int_equip_row["bookings"] += 1

    booking_count = len(revenue_rows)
    avg_booking_value = (total_revenue / booking_count) if booking_count else Decimal("0.00")

    refunded_amount = abs(
        _dec(bookings_qs.filter(status=BookingStatus.REFUNDED).aggregate(total=Sum("total_charge"))["total"])
    )

    pending_payments = _dec(
        bookings_qs.filter(status=BookingStatus.PENDING_PAYMENT).aggregate(total=Sum("amount_due"))["total"]
    )
    outstanding_dues = _dec(
        bookings_qs.filter(amount_due__gt=0, payment_settled_at__isnull=True)
        .exclude(status__in=_NON_DUE_STATUSES)
        .aggregate(total=Sum("amount_due"))["total"]
    )

    # Wallet recharges scoped to the department, bucketed by status within the date range.
    recharge_qs = WalletRechargeRequest.objects.filter(department_id=department_id)

    def _responded_in_range(status_value: str):
        return recharge_qs.filter(status=status_value).filter(
            Q(responded_at__date__gte=date_from, responded_at__date__lte=date_to)
            | Q(responded_at__isnull=True, created_at__date__gte=date_from, created_at__date__lte=date_to)
        )

    approved_recharges = _responded_in_range(WalletRechargeRequestStatus.APPROVED)
    pending_recharges = recharge_qs.filter(
        status=WalletRechargeRequestStatus.PENDING,
        created_at__date__gte=date_from,
        created_at__date__lte=date_to,
    )
    rejected_recharges = _responded_in_range(WalletRechargeRequestStatus.REJECTED)
    cancelled_recharges = _responded_in_range(WalletRechargeRequestStatus.CANCELLED)

    wallet_recharge_approved = _dec(approved_recharges.aggregate(total=Sum("amount"))["total"])
    wallet_recharge_pending = _dec(pending_recharges.aggregate(total=Sum("amount"))["total"])
    wallet_recharge_rejected = _dec(rejected_recharges.aggregate(total=Sum("amount"))["total"])
    wallet_recharge_cancelled = _dec(cancelled_recharges.aggregate(total=Sum("amount"))["total"])

    project_grant = _dec(
        approved_recharges.filter(recharge_mode=WalletRechargeMode.PROJECT_GRANT).aggregate(total=Sum("amount"))[
            "total"
        ]
    )
    direct_cash_deposit = _dec(
        approved_recharges.filter(recharge_mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT).aggregate(
            total=Sum("amount")
        )["total"]
    )

    online_payments = Decimal("0.00")
    try:
        online_payments = _dec(
            PaymentGatewayTransaction.objects.filter(
                department_id=department_id,
                status=PaymentGatewayStatus.SUCCESS,
                created_at__date__gte=date_from,
                created_at__date__lte=date_to,
            ).aggregate(total=Sum("amount"))["total"]
        )
    except Exception:
        online_payments = Decimal("0.00")

    # Razorpay PaymentOrder totals (base amounts settled) + pending settlements UTRs
    razorpay_online = Decimal("0.00")
    razorpay_pending_settlements = Decimal("0.00")
    try:
        from iic_booking.payments.models import PaymentOrder, PaymentOrderStatus, PaymentSettlement

        razorpay_online = _dec(
            PaymentOrder.objects.filter(
                department_id=department_id,
                status=PaymentOrderStatus.PAID,
                created_at__date__gte=date_from,
                created_at__date__lte=date_to,
            ).aggregate(total=Sum("base_amount"))["total"]
        )
        online_payments = online_payments + razorpay_online
        razorpay_pending_settlements = _dec(
            PaymentSettlement.objects.filter(bank_utr="")
            .filter(settled_on__gte=date_from, settled_on__lte=date_to)
            .aggregate(total=Sum("amount"))["total"]
        )
    except Exception:
        pass

    revenue_by_department = _sorted_revenue_rows(dept_bucket)
    revenue_by_org_category = _sorted_revenue_rows(org_bucket)
    total_revenue_f = _f(total_revenue)
    for row in revenue_by_org_category:
        row["pct"] = round((row["revenue"] / total_revenue_f * 100), 2) if total_revenue_f else 0.0

    equipment_revenue = _sorted_revenue_rows(equip_bucket)[:50]
    internal_by_department = _sorted_revenue_rows(internal_dept_bucket)
    internal_by_equipment = _sorted_revenue_rows(internal_equip_bucket)[:50]
    external_by_category = _sorted_revenue_rows(external_cat_bucket)

    equipment_analysis = sorted(
        (
            {
                "name": v["name"],
                "code": v["code"],
                "revenue": _f(v["revenue"]),
                "bookings": v["bookings"],
                "booking_hours": round(v["hours"], 2),
            }
            for v in equip_hours_bucket.values()
        ),
        key=lambda r: -r["revenue"],
    )[:50]

    # Revenue trend: daily buckets for ranges up to ~2 months, monthly buckets otherwise.
    span_days = (date_to - date_from).days
    revenue_trend: list[dict[str, Any]] = []
    if span_days <= 62:
        d = date_from
        while d <= date_to:
            revenue_trend.append({"date": d.isoformat(), "revenue": _f(trend_bucket_by_day.get(d, Decimal("0.00")))})
            d += timedelta(days=1)
    else:
        monthly: dict[str, Decimal] = defaultdict(Decimal)
        for d, amt in trend_bucket_by_day.items():
            monthly[d.strftime("%Y-%m")] += amt
        for key in sorted(monthly.keys()):
            revenue_trend.append({"date": key, "revenue": _f(monthly[key])})

    internal_vs_external = [
        {"name": "Internal", "value": _f(internal_revenue)},
        {"name": "External", "value": _f(external_revenue)},
    ]

    department = getattr(user, "department", None)
    department_name = getattr(department, "name", "") or ""
    report_title = (
        f"Departmental Finance Report — {department_name}" if department_name else "Departmental Finance Report"
    )

    return {
        "meta": {
            "institute_name": _INSTITUTE_NAME,
            "report_title": report_title,
            "date_from": date_from.isoformat(),
            "date_to": date_to.isoformat(),
            "generated_at": timezone.now().isoformat(),
            "generated_by": _generated_by_display(user),
        },
        "summary": {
            "total_revenue": total_revenue_f,
            "internal_revenue": _f(internal_revenue),
            "external_revenue": _f(external_revenue),
            "wallet_recharges": _f(wallet_recharge_approved),
            "pending_payments": _f(pending_payments),
            "refunded_amount": _f(refunded_amount),
            "outstanding_dues": _f(outstanding_dues),
            "booking_count": booking_count,
            "avg_booking_value": _f(avg_booking_value),
        },
        "charts": {
            "revenue_trend": revenue_trend,
            "internal_vs_external": internal_vs_external,
            "revenue_by_department": revenue_by_department,
            "revenue_by_org_category": revenue_by_org_category,
            "equipment_revenue": equipment_revenue,
        },
        "tables": {
            "internal_by_department": internal_by_department,
            "internal_by_equipment": internal_by_equipment,
            "external_by_category": external_by_category,
            "equipment_analysis": equipment_analysis,
            "payment_analytics": {
                "wallet_recharge_approved": _f(wallet_recharge_approved),
                "wallet_recharge_pending": _f(wallet_recharge_pending),
                "wallet_recharge_rejected": _f(wallet_recharge_rejected),
                "wallet_recharge_cancelled": _f(wallet_recharge_cancelled),
                "project_grant": _f(project_grant),
                "direct_cash_deposit": _f(direct_cash_deposit),
                "online_payments": _f(online_payments),
                "razorpay_online": _f(razorpay_online),
                "razorpay_pending_settlements": _f(razorpay_pending_settlements),
            },
        },
    }
