"""Shared definitions and aggregation for Reports & Statistics.

Every widget on a reports page must be computed from the same scoped queryset with these
definitions, so the status breakdown always adds up to the booking total and the money cards
agree with the per-member / per-equipment tables.

Definitions
- Total bookings: every booking in scope, any status.
- Status counts: the same bookings grouped by ``status``; they always sum to the total.
- Charged bookings: statuses whose charge stays debited from the wallet (``CHARGED_STATUSES``).
- Total spent: sum of ``total_charge`` over charged bookings (partial cancellations already
  reduce ``total_charge``; fully refunded / cancelled / waitlisted / unpaid bookings are excluded).
- Hours booked: ``total_time_minutes`` of charged bookings, in hours.
- Average cost: total spent / charged bookings.
- Refunded amount: ``total_charge`` of bookings that ended fully refunded (``REFUNDED_STATUSES``).
"""

from __future__ import annotations

import logging
from collections import Counter
from decimal import Decimal
from typing import Any, Optional

from django.db.models import Count, Q, QuerySet, Sum

from .models import Booking, BookingStatus

logger = logging.getLogger(__name__)

CHARGED_STATUSES = frozenset(
    {
        BookingStatus.BOOKED,
        BookingStatus.DISRUPTION_PENDING,
        BookingStatus.HOLD,
        BookingStatus.PROCESSING,
        BookingStatus.COMPLETED,
        BookingStatus.BOOKING_NOT_UTILIZED,
    }
)

# Statuses reached only after the full charge was credited back to the wallet.
REFUNDED_STATUSES = frozenset(
    {
        BookingStatus.REFUNDED,
        BookingStatus.ABSENT,
        BookingStatus.UNDER_MAINTENANCE,
        BookingStatus.OTHER_DISRUPTION,
    }
)

SCOPE_PERSONAL = "personal"
SCOPE_WALLET_GROUP = "wallet_group"
SCOPE_EQUIPMENT = "equipment"
SCOPE_DEPARTMENT = "department"
SCOPE_INSTITUTE = "institute"


def report_bookings_scope(user) -> tuple[QuerySet, str]:
    """Bookings visible on the user's Reports & Statistics page and the scope label.

    Same scoping as the My Bookings list: own (+ approved linked students for wallet owners),
    OIC / Lab Operator equipment, department admin department, admin everything. Uses only
    single-valued filters so the queryset never needs ``distinct()``.
    """
    from iic_booking.equipment.api_views import (
        _get_equipment_ids_for_log_access,
        check_operator_permission,
    )
    from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
    from iic_booking.users.test_accounts import exclude_test_bookings, is_test_user

    qs = Booking.objects.all()
    user_type = getattr(user, "user_type", None)

    if user_type == UserType.DEPT_ADMIN:
        dept_id = getattr(user, "department_id", None)
        qs = qs.filter(equipment__internal_department_id=dept_id) if dept_id else qs.none()
        scope = SCOPE_DEPARTMENT
    elif not check_operator_permission(user):
        student_ids = list(
            WalletJoinRequest.objects.filter(
                faculty=user, status=WalletJoinRequestStatus.APPROVED
            ).values_list("student_id", flat=True)
        )
        if student_ids:
            qs = qs.filter(Q(user=user) | Q(user_id__in=student_ids))
            scope = SCOPE_WALLET_GROUP
        else:
            qs = qs.filter(user=user)
            scope = SCOPE_PERSONAL
        # Test accounts still see their own activity; real users never see test bookings.
        if is_test_user(user):
            return qs, scope
    elif user_type == UserType.MANAGER:
        ids = get_equipment_ids_managed_by_oic(user.id)
        qs = qs.filter(equipment_id__in=ids) if ids else qs.none()
        scope = SCOPE_EQUIPMENT
    elif user_type == UserType.OPERATOR:
        ids = _get_equipment_ids_for_log_access(user) or []
        qs = qs.filter(equipment_id__in=ids) if ids else qs.none()
        scope = SCOPE_EQUIPMENT
    else:
        scope = SCOPE_INSTITUTE

    return exclude_test_bookings(qs), scope


def status_counts_for(qs: QuerySet) -> dict[str, int]:
    """Bookings per status. Ordering is cleared so GROUP BY is only ``status``."""
    counts: Counter[str] = Counter()
    for row in qs.order_by().values("status").annotate(n=Count("pk")):
        counts[row["status"] or "UNKNOWN"] += int(row["n"] or 0)
    return dict(counts)


def summarize_report_bookings(qs: QuerySet) -> dict[str, Any]:
    """Aggregate the report cards and status breakdown from one queryset."""
    qs = qs.order_by()
    total = qs.count()
    status_counts = status_counts_for(qs)
    if sum(status_counts.values()) != total:
        logger.error(
            "booking report status breakdown %s does not add up to total %s; recounting",
            sum(status_counts.values()),
            total,
        )
        status_counts = dict(Counter(s or "UNKNOWN" for s in qs.values_list("status", flat=True)))
        total = sum(status_counts.values())

    charged = qs.filter(status__in=CHARGED_STATUSES).aggregate(
        n=Count("pk"), spent=Sum("total_charge"), minutes=Sum("total_time_minutes")
    )
    refunded = qs.filter(status__in=REFUNDED_STATUSES).aggregate(amount=Sum("total_charge"))

    charged_n = int(charged["n"] or 0)
    spent = Decimal(str(charged["spent"] or 0)).quantize(Decimal("0.01"))
    hours = float(charged["minutes"] or 0) / 60.0
    return {
        "total_bookings": total,
        "status_counts": dict(sorted(status_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
        "charged_bookings": charged_n,
        "total_spent": float(spent),
        "total_hours": round(hours, 2),
        "average_cost": round(float(spent) / charged_n, 2) if charged_n else 0.0,
        "refunded_amount": float(Decimal(str(refunded["amount"] or 0)).quantize(Decimal("0.01"))),
        "status_sum_matches_total": sum(status_counts.values()) == total,
    }


def filter_report_period(
    qs: QuerySet,
    date_from: Optional[Any] = None,
    date_to: Optional[Any] = None,
) -> QuerySet:
    """Restrict to bookings created on [date_from, date_to] (IST calendar days, inclusive)."""
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    return qs
