"""The "My activity" panel on the dashboard of internal and external users: what they need day to day, in one call.

Only the signed-in user's own bookings. Spending counts bookings that were charged and not cancelled or refunded
(booked, processing, completed or not utilised), by the month they were made.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

from django.db.models import Count, Exists, Max, OuterRef, Q, Sum
from django.db.models.functions import TruncMonth
from django.utils import timezone
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .admin_insights.common import iso, money

UPCOMING_LIMIT = 6
RESULTS_LIMIT = 4
BOOK_AGAIN_LIMIT = 4
SPEND_MONTHS = 6
# Waiting on the slot, on payment or on the user's choice.
ACTIVE_STATUSES = ("PENDING", "PENDING_PAYMENT", "WAITLISTED", "BOOKED", "DISRUPTION_PENDING", "HOLD")
SPEND_STATUSES = ("BOOKED", "PROCESSING", "COMPLETED", "BOOKING_NOT_UTILIZED")


def _month_start(d):
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _shift_months(d, months: int):
    y, m = divmod(d.month - 1 + months, 12)
    return d.replace(year=d.year + y, month=m + 1)


def _row(b, *, with_deadline: bool = False) -> dict[str, Any]:
    row = {
        "id": b.booking_id,
        "display_id": b.virtual_booking_id or f"{b.equipment.code}-{b.booking_id}",
        "equipment": {"id": b.equipment_id, "name": b.equipment.name, "code": b.equipment.code},
        "status": b.status,
        "status_display": b.get_status_display(),
        "slot_start": iso(getattr(b, "slot_start", None)),
        "slot_end": iso(getattr(b, "slot_end", None)),
        "charge": money(b.total_charge),
    }
    if with_deadline:
        from .sample_submission_deadline_reminders import compute_sample_submission_deadline

        try:
            row["sample_deadline"] = iso(compute_sample_submission_deadline(b))
        except Exception:  # a deadline is a hint; never fail the panel over it
            row["sample_deadline"] = None
    return row


def _wallet(user) -> dict[str, Any] | None:
    from iic_booking.users.models.wallet import SubWallet

    try:
        wallet = user.get_accessible_wallet()
    except Exception:
        wallet = None
    if wallet is None:
        return None
    balance = SubWallet.objects.filter(wallet=wallet).aggregate(s=Sum("balance"))["s"] or Decimal("0")
    return {"balance": money(balance), "is_owner": wallet.user_id == user.pk}


def build_my_activity(user) -> dict[str, Any]:
    from .admin_insights.user_card import _slot_annotations
    from .models import Booking, BookingResultFile, BookingResultView

    now = timezone.now()
    local_now = timezone.localtime(now)
    mine = Booking.objects.filter(user=user).select_related("equipment")

    upcoming_qs = (
        mine.filter(status__in=ACTIVE_STATUSES)
        .annotate(**_slot_annotations())
        .filter(Q(slot_end__gte=now) | Q(slot_end__isnull=True))
        .order_by("slot_start", "booking_id")
    )
    upcoming = list(upcoming_qs[:UPCOMING_LIMIT])
    rows = [_row(b, with_deadline=True) for b in upcoming]
    next_booking = next((r for r in rows if r["slot_start"] and r["status"] == "BOOKED"), None)

    counts = mine.aggregate(
        awaiting_payment=Count("pk", filter=Q(status="PENDING_PAYMENT")),
        needs_choice=Count("pk", filter=Q(status="DISRUPTION_PENDING")),
        waitlisted=Count("pk", filter=Q(status="WAITLISTED")),
        processing=Count("pk", filter=Q(status="PROCESSING")),
        completed_month=Count("pk", filter=Q(status="COMPLETED", updated_at__gte=_month_start(local_now))),
    )
    counts["upcoming"] = upcoming_qs.count()

    seen = BookingResultView.objects.filter(booking_id=OuterRef("pk"), user=user)
    with_results = (
        mine.filter(status="COMPLETED", result_files__isnull=False)
        .annotate(latest_file=Max("result_files__created_at"), files=Count("result_files"), seen=Exists(seen))
        .order_by("-latest_file")
    )
    recent_results = [
        {**_row(b), "files": b.files, "ready_at": iso(b.latest_file), "new": not b.seen}
        for b in with_results[:RESULTS_LIMIT]
    ]
    new_results = with_results.filter(seen=False).count()

    spend_from = _shift_months(_month_start(local_now), -(SPEND_MONTHS - 1))
    charged = mine.filter(status__in=SPEND_STATUSES)
    by_month = {
        r["month"].strftime("%Y-%m"): r  # TruncMonth works in the current time zone
        for r in charged.filter(created_at__gte=spend_from)
        .annotate(month=TruncMonth("created_at"))
        .values("month")
        .annotate(amount=Sum("total_charge"), minutes=Sum("total_time_minutes"), bookings=Count("pk"))
    }
    months = []
    for i in range(SPEND_MONTHS):
        key = _shift_months(spend_from, i).strftime("%Y-%m")
        r = by_month.get(key) or {}
        months.append({"month": key, "amount": money(r.get("amount")), "bookings": r.get("bookings") or 0})
    this_key, last_key = months[-1]["month"], months[-2]["month"]
    year_start = local_now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    year = charged.filter(created_at__gte=year_start).aggregate(amount=Sum("total_charge"), minutes=Sum("total_time_minutes"))

    top = (
        mine.filter(created_at__gte=now - timedelta(days=365))
        .exclude(status__in=("CANCELLED", "REFUNDED"))
        .values("equipment_id", "equipment__name", "equipment__code", "equipment__status")
        .annotate(times=Count("pk"), last=Max("created_at"))
        .order_by("-times", "-last")[:BOOK_AGAIN_LIMIT]
    )

    return {
        "generated_at": iso(now),
        "next": next_booking,
        "upcoming": rows,
        "counts": counts,
        "results": {"new": new_results, "recent": recent_results},
        "spend": {
            "this_month": next(m["amount"] for m in months if m["month"] == this_key),
            "last_month": next(m["amount"] for m in months if m["month"] == last_key),
            "year": money(year["amount"]),
            "months": months,
        },
        "hours": {
            "this_month": round(((by_month.get(this_key) or {}).get("minutes") or 0) / 60, 1),
            "year": round((year["minutes"] or 0) / 60, 1),
        },
        "wallet": _wallet(user),
        "book_again": [
            {
                "id": t["equipment_id"],
                "name": t["equipment__name"],
                "code": t["equipment__code"],
                "times": t["times"],
                "last_booked": iso(t["last"]),
                "available": (t["equipment__status"] or "ACTIVE") == "ACTIVE",
            }
            for t in top
        ],
    }


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_activity(request):
    """GET /api/profiles/me/activity/ — the signed-in user's own activity summary for the dashboard."""
    return Response(build_my_activity(request.user))
