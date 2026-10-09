"""Whitelisted ``ordering`` values for the booking list (View Booking / My Bookings column sorting)."""

from django.db.models import CharField, F, OuterRef, Subquery, Value
from django.db.models.functions import Coalesce, Lower, NullIf

from .models import BookingSlotRange, DailySlot

DEFAULT_BOOKING_LIST_ORDERING = "-created_at"

# Plain model fields (kept for existing callers).
_FIELD_KEYS = {
    "booking_id": "booking_id",
    "created_at": "created_at",
    "updated_at": "updated_at",
    "status": "status",
    "total_charge": "total_charge",
    "total_time_minutes": "total_time_minutes",
    "duration": "total_time_minutes",
    "rating": "rating",
}


def _first_slot_start():
    return Subquery(
        DailySlot.objects.filter(booking_id=OuterRef("pk")).order_by("start_datetime").values("start_datetime")[:1]
    )


def _last_slot_end():
    return Subquery(
        DailySlot.objects.filter(booking_id=OuterRef("pk")).order_by("-end_datetime").values("end_datetime")[:1]
    )


def _released_range(field: str):
    return Subquery(BookingSlotRange.objects.filter(booking_id=OuterRef("pk")).values(field)[:1])


def _supervisor_name():
    """Same rule as the Supervisor Name column: wallet owner of an approved join request (students / other)."""
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    qs = (
        WalletJoinRequest.objects.filter(
            student_id=OuterRef("user_id"),
            status=WalletJoinRequestStatus.APPROVED,
            student__user_type__in=[UserType.STUDENT, UserType.OTHER],
        )
        .exclude(wallet__user_id=OuterRef("user_id"))
        .annotate(
            _owner=Coalesce(
                NullIf("wallet__user__name", Value("")), "wallet__user__email", output_field=CharField()
            )
        )
    )
    if not qs.ordered:
        qs = qs.order_by("pk")
    return Lower(Subquery(qs.values("_owner")[:1]))


_EXPRESSION_KEYS = {
    "start_time": lambda: Coalesce(_first_slot_start(), _released_range("start_datetime")),
    "end_time": lambda: Coalesce(_last_slot_end(), _released_range("end_datetime")),
    "equipment_name": lambda: Lower("equipment__name"),
    "equipment_code": lambda: Lower("equipment__code"),
    "user_name": lambda: Lower("user__name"),
    "user_email": lambda: Lower("user__email"),
    "user_phone": lambda: F("user__phone_number"),
    "supervisor_name": _supervisor_name,
}

BOOKING_LIST_ORDERING_KEYS = frozenset(_FIELD_KEYS) | frozenset(_EXPRESSION_KEYS) | {"booking_ref"}


def apply_booking_list_ordering(queryset, ordering: str | None):
    """Order the booking list by a whitelisted key (``key`` / ``-key``); unknown values use ``-created_at``.

    Empty values sort last in both directions; ties fall back to newest booking first so
    pagination stays stable.
    """
    raw = (ordering or "").strip()
    desc = raw.startswith("-")
    key = raw[1:] if desc else raw
    if key not in BOOKING_LIST_ORDERING_KEYS:
        return queryset.order_by(DEFAULT_BOOKING_LIST_ORDERING)

    def _direction(expr):
        return expr.desc(nulls_last=True) if desc else expr.asc(nulls_last=True)

    if key == "booking_ref":
        # Displayed Booking ID: virtual id (e.g. "IICPXRD [A]202600005"), else "<equipment code>-#<pk>".
        queryset = queryset.annotate(_list_sort_key=NullIf(F("virtual_booking_id"), Value("")))
        return queryset.order_by(
            _direction(F("_list_sort_key")),
            _direction(F("equipment__code")),
            _direction(F("booking_id")),
        )
    if key == "booking_id":
        return queryset.order_by(_direction(F("booking_id")))
    if key in _FIELD_KEYS:
        return queryset.order_by(_direction(F(_FIELD_KEYS[key])), "-booking_id")
    queryset = queryset.annotate(_list_sort_key=_EXPRESSION_KEYS[key]())
    return queryset.order_by(_direction(F("_list_sort_key")), "-booking_id")
