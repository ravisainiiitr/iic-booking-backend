"""Figures behind the monthly usage report emails (previous calendar month, IST; compared with the month before).

Each section uses the definitions of the portal page it links to, so a number in the email matches what the
recipient sees after clicking through:

- Faculty (IITR Faculty wallet owners) — Reports & Statistics / faculty wallet expense report: the owner's and
  approved linked members' bookings *created* in the month; spend and hours count charged bookings
  (``CHARGED_STATUSES``); wallet credits are classified like the expense report (refund / recharge); recharges are
  approved recharge requests answered in the month.
- Officer in Charge — Equipment Performance Report: bookings with a slot in the month, revenue from completed
  bookings (test-wallet bookings left out), utilisation from the shared ``compute_utilization`` engine (counted from
  the portal go-live date), ratings submitted in the month, pending work now (``admin_insights.proficiency``).
- Head of Department — the same equipment figures for the department's equipment, plus bookings by the
  department's people on any equipment.

Test data is left out with ``iic_booking.equipment.testdata`` (test users and test equipment) unless the recipient is
a test account themselves (they see their own test activity, like on Reports & Statistics).
"""

from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from django.db.models import Count, F, OuterRef, Q, QuerySet, Subquery, Sum
from django.utils import timezone

IST = ZoneInfo("Asia/Kolkata")
ZERO = Decimal("0.00")
TOP_ROWS = 5
EQUIPMENT_ROWS = 15


# --------------------------------------------------------------------------- period


@dataclass(frozen=True)
class ReportMonth:
    """A calendar month in IST."""

    year: int
    month: int

    @classmethod
    def parse(cls, value: str) -> "ReportMonth":
        """``"YYYY-MM"``; ``ValueError`` otherwise."""
        try:
            year, month = (int(p) for p in str(value).strip().split("-")[:2])
            date(year, month, 1)
        except (TypeError, ValueError):
            raise ValueError("Month must look like 2026-09.") from None
        return cls(year, month)

    @classmethod
    def previous_to(cls, at: Optional[datetime] = None) -> "ReportMonth":
        """The calendar month before the one ``at`` (default now) falls in, in IST."""
        local = (at or timezone.now()).astimezone(IST)
        first = local.date().replace(day=1) - timedelta(days=1)
        return cls(first.year, first.month)

    @property
    def start(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def end(self) -> date:
        return date(self.year, self.month, monthrange(self.year, self.month)[1])

    @property
    def start_at(self) -> datetime:
        return datetime.combine(self.start, time.min, tzinfo=IST)

    @property
    def end_at(self) -> datetime:
        """Exclusive: midnight IST on the 1st of the next month."""
        return datetime.combine(self.end + timedelta(days=1), time.min, tzinfo=IST)

    @property
    def previous(self) -> "ReportMonth":
        last = self.start - timedelta(days=1)
        return ReportMonth(last.year, last.month)

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def label(self) -> str:
        return self.start.strftime("%B %Y")

    def reports_query(self, equipment: str = "all") -> str:
        return f"from={self.start.isoformat()}&to={self.end.isoformat()}&eq={equipment}"


# --------------------------------------------------------------------------- small helpers


def money(value) -> Decimal:
    return Decimal(str(value or 0)).quantize(Decimal("0.01"))


def hours(minutes) -> float:
    return round(float(minutes or 0) / 60.0, 1)


def _without_test_bookings(qs: QuerySet, include_test: bool, prefix: str = "") -> QuerySet:
    from iic_booking.equipment.testdata import exclude_test_bookings

    return qs if include_test else exclude_test_bookings(qs, prefix)


def _person(name, user_type, email) -> str:
    from iic_booking.users.display import format_named_person

    return format_named_person(name, user_type, email) or "—"


def _user_label(user) -> str:
    from iic_booking.users.display import get_user_display_name

    return get_user_display_name(user) or "—"


def is_test_recipient(user) -> bool:
    from iic_booking.equipment.testdata import is_test_user

    return is_test_user(user)


# --------------------------------------------------------------------------- faculty


def _charged(qs: QuerySet) -> QuerySet:
    from iic_booking.equipment.booking_report_metrics import CHARGED_STATUSES

    return qs.filter(status__in=CHARGED_STATUSES)


def _faculty_totals(member_ids: Iterable[int], month: ReportMonth, include_test: bool) -> dict[str, Any]:
    from iic_booking.equipment.models import Booking

    bookings = _without_test_bookings(
        Booking.objects.filter(user_id__in=list(member_ids), created_at__gte=month.start_at, created_at__lt=month.end_at),
        include_test,
    ).order_by()
    charged = _charged(bookings).aggregate(n=Count("pk"), spent=Sum("total_charge"), minutes=Sum("total_time_minutes"))
    return {
        "bookings": bookings.count(),
        "charged_bookings": int(charged["n"] or 0),
        "spend": money(charged["spent"]),
        "hours": hours(charged["minutes"]),
        "_qs": bookings,
    }


def _results_awaiting_download(member_ids: Iterable[int], include_test: bool) -> int:
    """Completed bookings with results their own booker has not downloaded since the last file arrived."""
    from iic_booking.equipment.models import Booking, BookingResultFile, BookingResultView, BookingStatus
    from iic_booking.equipment.pending_actions import RECENT_RESULTS_DAYS
    from iic_booking.equipment.results_sharing_views import _bookings_with_results

    cutoff = timezone.now() - timedelta(days=RECENT_RESULTS_DAYS)
    last_file = BookingResultFile.objects.filter(booking_id=OuterRef("pk")).order_by("-created_at").values("created_at")[:1]
    viewed = BookingResultView.objects.filter(booking_id=OuterRef("pk"), user_id=OuterRef("user_id")).values(
        "last_viewed_at"
    )[:1]
    qs = _without_test_bookings(
        Booking.objects.filter(user_id__in=list(member_ids), status=BookingStatus.COMPLETED), include_test
    )
    return (
        _bookings_with_results(qs)
        .annotate(last_result_file_at=Subquery(last_file), viewed_at=Subquery(viewed))
        .filter(
            Q(completed_at__gte=cutoff) | Q(results_available_notified_at__gte=cutoff) | Q(last_result_file_at__gte=cutoff)
        )
        .filter(Q(viewed_at__isnull=True) | Q(last_result_file_at__gt=F("viewed_at")))
        .count()
    )


def _sub_wallet_rows(wallet, month: ReportMonth, include_test: bool) -> list[dict[str, Any]]:
    from iic_booking.users.faculty_wallet_report import _classify_credit
    from iic_booking.users.models.wallet import SubWalletTransaction, WalletRechargeRequest, WalletRechargeRequestStatus
    from iic_booking.users.test_accounts import exclude_test_recharge_requests, exclude_test_wallet_txns

    sub_wallets = list(wallet.sub_wallets.select_related("department").order_by("department__name"))
    txns = SubWalletTransaction.objects.filter(
        sub_wallet__in=sub_wallets, created_at__gte=month.start_at, created_at__lt=month.end_at
    )
    if not include_test:
        txns = exclude_test_wallet_txns(txns)
    debits: dict[int, Decimal] = defaultdict(lambda: ZERO)
    refunds: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for sw_id, amount, description, kind in txns.values_list("sub_wallet_id", "amount", "description", "transaction_type"):
        if kind == SubWalletTransaction.TransactionType.DEBIT:
            debits[sw_id] += money(amount)
        elif _classify_credit(money(amount), description) == "refund":
            refunds[sw_id] += money(amount)

    recharges = WalletRechargeRequest.objects.filter(
        wallet=wallet, status=WalletRechargeRequestStatus.APPROVED, is_deleted=False
    ).filter(
        Q(responded_at__gte=month.start_at, responded_at__lt=month.end_at)
        | Q(responded_at__isnull=True, updated_at__gte=month.start_at, updated_at__lt=month.end_at)
    )
    if not include_test:
        recharges = exclude_test_recharge_requests(recharges)
    recharged: dict[Optional[int], Decimal] = defaultdict(lambda: ZERO)
    recharge_count: dict[Optional[int], int] = defaultdict(int)
    for dept_id, amount in recharges.values_list("department_id", "amount"):
        recharged[dept_id] += money(amount)
        recharge_count[dept_id] += 1

    rows = []
    for sw in sub_wallets:
        debited, refunded = debits[sw.id], refunds[sw.id]
        rows.append(
            {
                "name": getattr(sw.department, "name", "") or "—",
                "balance": money(sw.balance),
                "debited": debited,
                "refunded": refunded,
                "net_spend": debited - refunded,
                "recharged": recharged.get(sw.department_id, ZERO),
                "recharge_count": recharge_count.get(sw.department_id, 0),
            }
        )
    return rows


def _spending_limits(links, month: ReportMonth) -> list[dict[str, Any]]:
    from iic_booking.users.student_spending_limits import student_spend

    rows = []
    for link in links:
        if not link.spending_limit_enabled or link.monthly_limit_inr is None:
            continue
        spent = student_spend(link, month.start_at, month.end_at)
        limit = money(link.monthly_limit_inr)
        rows.append(
            {
                "name": _user_label(link.student),
                "spent": spent,
                "limit": limit,
                "percent": round(float(spent / limit) * 100) if limit > 0 else None,
            }
        )
    rows.sort(key=lambda r: -(r["percent"] or 0))
    return rows


def faculty_section(user, month: ReportMonth, *, include_test: Optional[bool] = None) -> Optional[dict[str, Any]]:
    """Faculty wallet owner's month, or None when they have no wallet."""
    from iic_booking.users.models import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    include_test = is_test_recipient(user) if include_test is None else include_test
    wallet = Wallet.objects.filter(user=user).first()
    if wallet is None:
        return None
    links = list(
        WalletJoinRequest.objects.filter(wallet=wallet, status=WalletJoinRequestStatus.APPROVED).select_related("student")
    )
    if not include_test:
        links = [link for link in links if not getattr(link.student, "is_test_account", False)]
    student_ids = {link.student_id for link in links}
    member_ids = {user.pk} | student_ids

    current = _faculty_totals(member_ids, month, include_test)
    previous = _faculty_totals(member_ids, month.previous, include_test)
    charged = _charged(current.pop("_qs"))
    previous.pop("_qs")

    by_equipment = [
        {
            "name": r["equipment__name"] or "—",
            "code": r["equipment__code"] or "",
            "bookings": int(r["n"] or 0),
            "hours": hours(r["minutes"]),
            "spend": money(r["spent"]),
        }
        for r in charged.values("equipment_id", "equipment__name", "equipment__code")
        .annotate(n=Count("pk"), minutes=Sum("total_time_minutes"), spent=Sum("total_charge"))
        .order_by("-spent", "-n")[:EQUIPMENT_ROWS]
    ]
    own = charged.filter(user_id=user.pk).aggregate(n=Count("pk"), spent=Sum("total_charge"), minutes=Sum("total_time_minutes"))
    top_students = [
        {
            "name": _person(r["user__name"], r["user__user_type"], r["user__email"]),
            "bookings": int(r["n"] or 0),
            "hours": hours(r["minutes"]),
            "spend": money(r["spent"]),
        }
        for r in charged.filter(user_id__in=student_ids)
        .values("user_id", "user__name", "user__user_type", "user__email")
        .annotate(n=Count("pk"), minutes=Sum("total_time_minutes"), spent=Sum("total_charge"))
        .order_by("-spent", "-n")[:TOP_ROWS]
    ]
    sub_wallets = _sub_wallet_rows(wallet, month, include_test)
    recharged = sum((r["recharged"] for r in sub_wallets), ZERO)
    debited = sum((r["debited"] for r in sub_wallets), ZERO)
    section = {
        "audience": "faculty",
        "current": current,
        "previous": previous,
        "own": {"bookings": int(own["n"] or 0), "spend": money(own["spent"]), "hours": hours(own["minutes"])},
        "students_linked": len(student_ids),
        "by_equipment": by_equipment,
        "top_students": top_students,
        "sub_wallets": sub_wallets,
        "balance_total": sum((r["balance"] for r in sub_wallets), ZERO),
        "recharged_total": recharged,
        "recharge_count": sum(r["recharge_count"] for r in sub_wallets),
        "spending_limits": _spending_limits(links, month),
        "results_awaiting": _results_awaiting_download(member_ids, include_test),
        "links": {
            "reports": "/reports",
            "wallet": "/wallet",
            "results": "/my-results",
            "bookings": "/my-bookings",
        },
    }
    section["has_activity"] = bool(current["bookings"] or current["spend"] or recharged or debited)
    return section


# --------------------------------------------------------------------------- equipment (OIC / HoD)


def _equipment_rows_scope(equipment_ids: Iterable[int], include_test: bool) -> tuple[list, list[int], dict[int, int]]:
    """(parent / standalone equipment rows, their ids plus modes, mode id -> parent id), test equipment left out."""
    from iic_booking.equipment.mode_utils import expand_equipment_ids_for_mode_rollup
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.testdata import exclude_test_equipment, get_test_equipment_ids

    ids = sorted({int(i) for i in equipment_ids})
    if not ids:
        return [], [], {}
    expanded, rollup = expand_equipment_ids_for_mode_rollup(ids)
    if not include_test:
        test_ids = get_test_equipment_ids(expanded)
        expanded = [eid for eid in expanded if eid not in test_ids]
    parent_ids = sorted({rollup.get(eid, eid) for eid in ids if eid in set(expanded)})
    qs = Equipment.objects.filter(equipment_id__in=parent_ids)
    if not include_test:
        qs = exclude_test_equipment(qs)
    rows = list(qs.only("equipment_id", "name", "code").order_by("name"))
    return rows, expanded, rollup


def _bookings_with_slot_in(month: ReportMonth, eq_ids: list[int], include_test: bool) -> QuerySet:
    """Same rows as the Performance Report: a slot in the month, or (cancelled / refunded) a released range in it."""
    from iic_booking.equipment.models import Booking, DailySlot

    slot_booking_ids = DailySlot.objects.filter(
        date__gte=month.start, date__lte=month.end, booking__isnull=False
    ).values("booking_id")
    qs = Booking.objects.filter(equipment_id__in=eq_ids).filter(
        Q(booking_id__in=slot_booking_ids)
        | Q(
            released_slot_range__start_datetime__date__lte=month.end,
            released_slot_range__end_datetime__date__gte=month.start,
        )
    )
    return _without_test_bookings(qs, include_test).order_by()


def _revenue_qs(bookings: QuerySet, include_test: bool) -> QuerySet:
    from iic_booking.equipment.models import BookingStatus
    from iic_booking.users.test_accounts import exclude_test_revenue_bookings

    completed = bookings.filter(status=BookingStatus.COMPLETED)
    return completed if include_test else exclude_test_revenue_bookings(completed)


def _served(bookings: QuerySet) -> QuerySet:
    from iic_booking.equipment.reports import _SERVED_EXCLUDE_STATUSES

    return bookings.exclude(status__in=_SERVED_EXCLUDE_STATUSES)


def _by_parent(rows, rollup: dict[int, int]) -> dict[int, dict[str, Any]]:
    out: dict[int, dict[str, Any]] = defaultdict(dict)
    for eid, values in rows:
        target = out[rollup.get(eid, eid)]
        for key, value in values.items():
            target[key] = target.get(key, 0) + (value or 0)
    return out


def _equipment_month(rows, expanded: list[int], rollup: dict[int, int], month: ReportMonth,
                     include_test: bool) -> dict[str, Any]:
    """Per-equipment and total bookings, hours, revenue and utilisation for one month."""
    from iic_booking.equipment.utilization import UtilizationTally, compute_utilization_by_equipment

    bookings = _bookings_with_slot_in(month, expanded, include_test)
    counts = _by_parent(
        ((r["equipment_id"], {"bookings": r["n"]}) for r in bookings.values("equipment_id").annotate(n=Count("pk"))),
        rollup,
    )
    served = _by_parent(
        (
            (r["equipment_id"], {"minutes": r["m"]})
            for r in _served(bookings).values("equipment_id").annotate(m=Sum("total_time_minutes"))
        ),
        rollup,
    )
    revenue = _by_parent(
        (
            (r["equipment_id"], {"revenue": money(r["s"])})
            for r in _revenue_qs(bookings, include_test).values("equipment_id").annotate(s=Sum("total_charge"))
        ),
        rollup,
    )
    parent_ids = [e.equipment_id for e in rows]
    tallies = compute_utilization_by_equipment(parent_ids, month.start, month.end) if parent_ids else {}
    total = UtilizationTally()
    per = {}
    for eq in rows:
        eid = eq.equipment_id
        tally = tallies.get(eid) or UtilizationTally()
        total.merge(tally)
        per[eid] = {
            "bookings": int(counts.get(eid, {}).get("bookings", 0)),
            "hours": hours(served.get(eid, {}).get("minutes", 0)),
            "revenue": money(revenue.get(eid, {}).get("revenue", 0)),
            "utilization": tally.factor,
            "booked_hours": round(tally.booked_hours, 1),
            "available_hours": round(tally.available_hours, 1),
        }
    return {
        "per": per,
        "bookings": sum(p["bookings"] for p in per.values()),
        "hours": round(sum(p["hours"] for p in per.values()), 1),
        "revenue": sum((p["revenue"] for p in per.values()), ZERO),
        "utilization": total.factor,
        "_bookings": bookings,
    }


def _cancellations(expanded: list[int], rollup: dict[int, int], month: ReportMonth,
                   include_test: bool) -> dict[int, dict[str, Any]]:
    from iic_booking.equipment.cancellation_models import BookingCancellation

    qs = _without_test_bookings(
        BookingCancellation.objects.filter(
            booking__equipment_id__in=expanded, cancelled_at__gte=month.start_at, cancelled_at__lt=month.end_at
        ),
        include_test,
        "booking__",
    )
    return _by_parent(
        (
            (r["booking__equipment_id"], {"cancellations": r["n"], "refunded": money(r["refund"])})
            for r in qs.values("booking__equipment_id").annotate(n=Count("pk"), refund=Sum("refund_amount"))
        ),
        rollup,
    )


def _refunded_bookings(bookings: QuerySet, rollup: dict[int, int]) -> dict[int, dict[str, Any]]:
    from iic_booking.equipment.booking_report_metrics import REFUNDED_STATUSES

    return _by_parent(
        (
            (r["equipment_id"], {"refunds": r["n"]})
            for r in bookings.filter(status__in=REFUNDED_STATUSES).values("equipment_id").annotate(n=Count("pk"))
        ),
        rollup,
    )


def _disruptions(expanded: list[int], rollup: dict[int, int], month: ReportMonth) -> dict[int, dict[str, Any]]:
    from iic_booking.equipment.disruption_models import DisruptionEvent

    qs = DisruptionEvent.objects.filter(equipment_id__in=expanded, is_deleted=False, start_at__lt=month.end_at).filter(
        Q(end_at__isnull=True) | Q(end_at__gte=month.start_at)
    )
    return _by_parent(
        (
            (r["equipment_id"], {"disruptions": r["n"], "bookings_affected": r["affected"]})
            for r in qs.values("equipment_id").annotate(n=Count("pk"), affected=Sum("bookings_affected"))
        ),
        rollup,
    )


def _ratings(expanded: list[int], rollup: dict[int, int], month: ReportMonth,
             include_test: bool) -> dict[int, dict[str, Any]]:
    from iic_booking.equipment.models import Booking
    from iic_booking.equipment.rating_alerts import LOW_RATING_MAX

    qs = _without_test_bookings(
        Booking.objects.filter(
            equipment_id__in=expanded,
            rated_at__gte=month.start_at,
            rated_at__lt=month.end_at,
            rating_removed=False,
            rating__isnull=False,
        ),
        include_test,
    )
    return _by_parent(
        (
            (r["equipment_id"], {"ratings": r["n"], "rating_sum": r["total"], "low_ratings": r["low"]})
            for r in qs.values("equipment_id").annotate(
                n=Count("pk"), total=Sum("rating"), low=Count("pk", filter=Q(rating__lte=LOW_RATING_MAX))
            )
        ),
        rollup,
    )


def _pending_now(expanded: list[int], rollup: dict[int, int]) -> dict[int, dict[str, Any]]:
    """Work waiting on the equipment's staff right now (same queue as Staff proficiency)."""
    from iic_booking.equipment.admin_insights.proficiency import _pending

    items = _pending(expanded, timezone.now()) if expanded else {}
    return _by_parent(
        (
            (eid, {"pending": len(rows), "overdue": sum(1 for r in rows if r["overdue"])})
            for eid, rows in items.items()
        ),
        rollup,
    )


def operator_proficiency(expanded: list[int], month: ReportMonth) -> list[dict[str, Any]]:
    """Lab Operators of these equipment: handled in the month, pending / overdue now, average response, score."""
    from iic_booking.equipment.admin_insights.proficiency import (
        _assignments,
        _handled,
        _names,
        _pending,
        _person_rows,
    )
    from iic_booking.equipment.models import Equipment

    if not expanded:
        return []
    operators = _assignments(expanded)["operator"]
    if not operators:
        return []
    equipment = {
        e["equipment_id"]: e
        for e in Equipment.objects.filter(equipment_id__in=expanded).values("equipment_id", "name", "code")
    }
    pending = _pending(expanded, timezone.now())
    handled = _handled(expanded, set(operators), month.start_at, month.end_at)
    rows = _person_rows("operator", operators, pending, handled, _names(set(operators)), equipment)
    return [
        {
            "name": r["name"] or "—",
            "handled": r["handled"],
            "pending": r["pending"],
            "overdue": r["overdue"],
            "avg_response_hours": r["avg_response_hours"],
            "score": r["score"],
        }
        for r in rows
    ]


def equipment_section(equipment_ids: Iterable[int], month: ReportMonth, *, include_test: bool = False,
                      with_staff: bool = True) -> dict[str, Any]:
    """Per-equipment table and totals for this month and the one before."""
    rows, expanded, rollup = _equipment_rows_scope(equipment_ids, include_test)
    current = _equipment_month(rows, expanded, rollup, month, include_test)
    previous = _equipment_month(rows, expanded, rollup, month.previous, include_test)
    bookings = current.pop("_bookings")
    previous.pop("_bookings")
    cancellations = _cancellations(expanded, rollup, month, include_test)
    refunds = _refunded_bookings(bookings, rollup)
    disruptions = _disruptions(expanded, rollup, month)
    ratings = _ratings(expanded, rollup, month, include_test)
    pending = _pending_now(expanded, rollup) if with_staff else {}

    table = []
    for eq in rows:
        eid = eq.equipment_id
        rated = ratings.get(eid, {})
        n_ratings = int(rated.get("ratings", 0))
        table.append(
            {
                "equipment_id": eid,
                "name": eq.name or "—",
                "code": eq.code or "",
                **current["per"][eid],
                "previous_bookings": previous["per"][eid]["bookings"],
                "cancellations": int(cancellations.get(eid, {}).get("cancellations", 0)),
                "refunded": money(cancellations.get(eid, {}).get("refunded", 0)),
                "refunds": int(refunds.get(eid, {}).get("refunds", 0)),
                "disruptions": int(disruptions.get(eid, {}).get("disruptions", 0)),
                "bookings_affected": int(disruptions.get(eid, {}).get("bookings_affected", 0)),
                "ratings": n_ratings,
                "avg_rating": round(float(rated.get("rating_sum", 0)) / n_ratings, 1) if n_ratings else None,
                "low_ratings": int(rated.get("low_ratings", 0)),
                "pending": int(pending.get(eid, {}).get("pending", 0)),
                "overdue": int(pending.get(eid, {}).get("overdue", 0)),
                "link": f"/reports?{month.reports_query(str(eid))}",
            }
        )
    table.sort(key=lambda r: (-r["bookings"], -r["hours"], r["name"].lower()))
    total_ratings = sum(r["ratings"] for r in table)
    rating_sum = sum(float(ratings.get(r["equipment_id"], {}).get("rating_sum", 0)) for r in table)
    totals = {
        "equipment": len(table),
        "bookings": current["bookings"],
        "hours": current["hours"],
        "revenue": current["revenue"],
        "utilization": current["utilization"],
        "cancellations": sum(r["cancellations"] for r in table),
        "refunded": sum((r["refunded"] for r in table), ZERO),
        "refunds": sum(r["refunds"] for r in table),
        "disruptions": sum(r["disruptions"] for r in table),
        "ratings": total_ratings,
        "avg_rating": round(rating_sum / total_ratings, 1) if total_ratings else None,
        "low_ratings": sum(r["low_ratings"] for r in table),
        "pending": sum(r["pending"] for r in table),
        "overdue": sum(r["overdue"] for r in table),
    }
    return {
        "table": table,
        "totals": totals,
        "previous": {k: previous[k] for k in ("bookings", "hours", "revenue", "utilization")},
        "expanded": expanded,
        "rollup": rollup,
        "_bookings": bookings,
    }


def oic_equipment_ids(user) -> list[int]:
    from iic_booking.equipment.models import EquipmentManager

    return sorted(set(EquipmentManager.objects.filter(manager=user).values_list("equipment_id", flat=True)))


def oic_section(user, month: ReportMonth, *, include_test: Optional[bool] = None) -> Optional[dict[str, Any]]:
    """Officer in Charge's equipment month, or None when they are OIC of no (real) equipment."""
    include_test = is_test_recipient(user) if include_test is None else include_test
    ids = oic_equipment_ids(user)
    if not ids:
        return None
    data = equipment_section(ids, month, include_test=include_test)
    data.pop("_bookings")
    if not data["table"]:
        return None
    t = data["totals"]
    data.update(
        audience="oic",
        operators=operator_proficiency(data.pop("expanded"), month),
        links={
            "report": f"/reports?{month.reports_query()}",
            "pending": "/dashboard#bookings-awaiting-completion",
            "disruptions": "/disruptions",
            "bookings": "/booking-management",
        },
        has_activity=bool(t["bookings"] or t["cancellations"] or t["disruptions"] or t["ratings"] or t["pending"]),
    )
    data.pop("rollup")
    return data


# --------------------------------------------------------------------------- HoD


def hod_departments(user) -> list:
    """Departments the user heads now (active Head of Department assignment)."""
    from iic_booking.users.models.channel_i_identity import HeadOfDepartmentAssignment

    today = timezone.localdate()
    return [
        a.department
        for a in HeadOfDepartmentAssignment.objects.filter(user=user, active=True)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .select_related("department")
    ]


def _user_groups(bookings: QuerySet, include_test: bool) -> list[dict[str, Any]]:
    from iic_booking.users.user_categories import category_label

    served = defaultdict(lambda: {"bookings": 0, "minutes": 0, "revenue": ZERO})
    for r in bookings.values("user_type_snapshot").annotate(n=Count("pk")):
        served[category_label(r["user_type_snapshot"])]["bookings"] += int(r["n"] or 0)
    for r in _served(bookings).values("user_type_snapshot").annotate(m=Sum("total_time_minutes")):
        served[category_label(r["user_type_snapshot"])]["minutes"] += int(r["m"] or 0)
    for r in _revenue_qs(bookings, include_test).values("user_type_snapshot").annotate(s=Sum("total_charge")):
        served[category_label(r["user_type_snapshot"])]["revenue"] += money(r["s"])
    rows = [
        {"name": name, "bookings": v["bookings"], "hours": hours(v["minutes"]), "revenue": v["revenue"]}
        for name, v in served.items()
        if v["bookings"]
    ]
    rows.sort(key=lambda r: (-r["bookings"], -r["revenue"], r["name"]))
    return rows


def _department_people(department, month: ReportMonth, include_test: bool) -> dict[str, Any]:
    """Bookings by the department's people (any equipment), counted like Reports & Statistics (created in month)."""
    from iic_booking.equipment.models import Booking

    def totals(m: ReportMonth):
        qs = _without_test_bookings(
            Booking.objects.filter(user__department=department, created_at__gte=m.start_at, created_at__lt=m.end_at),
            include_test,
        ).order_by()
        charged = _charged(qs).aggregate(n=Count("pk"), spent=Sum("total_charge"), minutes=Sum("total_time_minutes"))
        return qs, {
            "bookings": qs.count(),
            "users": qs.values("user_id").distinct().count(),
            "spend": money(charged["spent"]),
            "hours": hours(charged["minutes"]),
        }

    qs, current = totals(month)
    _, previous = totals(month.previous)
    top_equipment = [
        {"name": r["equipment__name"] or "—", "code": r["equipment__code"] or "", "bookings": int(r["n"] or 0),
         "spend": money(r["spent"])}
        for r in _charged(qs).values("equipment__name", "equipment__code")
        .annotate(n=Count("pk"), spent=Sum("total_charge"))
        .order_by("-n", "-spent")[:TOP_ROWS]
    ]
    return {"current": current, "previous": previous, "top_equipment": top_equipment}


def hod_section(user, month: ReportMonth, *, include_test: Optional[bool] = None) -> Optional[dict[str, Any]]:
    """Department view for a Head of Department, or None when they head no department."""
    from iic_booking.equipment.models import Equipment

    include_test = is_test_recipient(user) if include_test is None else include_test
    departments = hod_departments(user)
    if not departments:
        return None
    out = []
    for dept in departments:
        ids = list(Equipment.objects.filter(internal_department=dept).values_list("equipment_id", flat=True))
        equipment = equipment_section(ids, month, include_test=include_test, with_staff=False) if ids else None
        groups = []
        if equipment is not None:
            groups = _user_groups(equipment.pop("_bookings"), include_test)
            equipment.pop("expanded")
            equipment.pop("rollup")
            if not equipment["table"]:
                equipment = None
        people = _department_people(dept, month, include_test)
        out.append(
            {
                "name": dept.name or "—",
                "equipment": equipment,
                "top_equipment": (equipment or {}).get("table", [])[:TOP_ROWS],
                "user_groups": groups,
                "people": people,
                "has_activity": bool((equipment and equipment["totals"]["bookings"]) or people["current"]["bookings"]),
            }
        )
    return {
        "audience": "hod",
        "departments": out,
        "links": {"report": f"/reports?{month.reports_query()}", "portal": "/dashboard"},
        "has_activity": any(d["has_activity"] for d in out),
    }
