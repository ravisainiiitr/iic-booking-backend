"""
Equipment quota management for internal (and external) bookings.

QuotaService is the single entry point for quota validation and usage
aggregation. QuotaChecker remains as a thin alias for older call sites.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import OuterRef, QuerySet, Subquery
from django.db.models.functions import Coalesce
from django.utils import timezone

from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

from .models import (
    Booking,
    BookingStatus,
    DailySlot,
    EquipmentGroupQuota,
    ExternalUserQuota,
    QuotaLimitType,
    QuotaType,
    UserTypeQuota,
)

# Bookings that hold (or held) the user's slots count toward quota:
# - awaiting payment / pending: the slots are reserved for the user;
# - awaiting the user's disruption choice: the user chose (or may choose) to wait and reschedule,
#   so the booking keeps its place in its original period until it is refunded;
# - Booking Not Utilized (no-show): the slots were consumed, no refund.
# Freed or facility-side outcomes never count: cancelled (with or without refund), refunded,
# operator unavailable, under maintenance / analysis not possible (refunded), waitlisted, urgent hold.
QUOTA_COUNTING_STATUSES = (
    BookingStatus.PENDING,
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.BOOKED,
    BookingStatus.DISRUPTION_PENDING,
    BookingStatus.PROCESSING,
    BookingStatus.COMPLETED,
    BookingStatus.BOOKING_NOT_UTILIZED,
)

QUOTA_EXCLUDED_STATUSES = (
    BookingStatus.WAITLISTED,
    BookingStatus.HOLD,
    BookingStatus.CANCELLED,
    BookingStatus.REFUNDED,
    BookingStatus.ABSENT,
    BookingStatus.UNDER_MAINTENANCE,
    BookingStatus.OTHER_DISRUPTION,
)


@dataclass(frozen=True)
class QuotaCheckResult:
    """Structured result for a single quota dimension check."""

    allowed: bool
    scope: str  # "Faculty Monthly" | "Faculty Weekly" | "Individual Monthly" | ...
    used_minutes: int
    requested_minutes: int
    limit_minutes: int
    remaining_before_request: int
    message: Optional[str] = None
    quota_type: str = ""
    # "group" (faculty wallet, shared), "individual" (one user) or "pool" (legacy per-user-type limit).
    scope_kind: str = ""
    period_start: Optional[datetime] = None
    period_end: Optional[datetime] = None
    members_count: int = 1

    def as_error(self) -> str:
        if self.message:
            return self.message
        projected = self.used_minutes + self.requested_minutes
        shared = (
            f" (shared across {self.members_count} user(s) on the faculty wallet)"
            if self.scope_kind == "group"
            else ""
        )
        return (
            f"{self.scope} quota exceeded: "
            f"current usage {self.used_minutes} min + requested {self.requested_minutes} min "
            f"= {projected} min; configured limit {self.limit_minutes} min; "
            f"remaining before this request {max(0, self.remaining_before_request)} min"
            f"{shared}."
        )

    def payload(self) -> dict:
        """What the booking UI needs to explain the refusal and open the breakdown."""
        return {
            "scope": self.scope_kind,
            "scope_label": self.scope,
            "period": self.quota_type,
            "period_start": self.period_start.isoformat() if self.period_start else None,
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "limit_minutes": int(self.limit_minutes),
            "used_minutes": int(self.used_minutes),
            "requested_minutes": int(self.requested_minutes),
            "remaining_minutes": max(0, int(self.limit_minutes) - int(self.used_minutes)),
            "over_by_minutes": max(0, int(self.used_minutes) + int(self.requested_minutes) - int(self.limit_minutes)),
            "members_count": int(self.members_count),
        }


@dataclass(frozen=True)
class QuotaDecision:
    """Outcome of the full quota pipeline; ``failure`` is set for minute limits so callers can explain it."""

    allowed: bool
    error: Optional[str] = None
    failure: Optional[QuotaCheckResult] = None

    def as_tuple(self) -> tuple[bool, Optional[str]]:
        return self.allowed, self.error


QUOTA_ALLOWED = QuotaDecision(True)


@dataclass(frozen=True)
class QuotaDimension:
    """
    One weekly or monthly minute limit that applies to a user, and whose bookings count toward it.
    Enforcement, the booking-page summary and the "bookings counted" breakdown all read usage through
    ``bookings_in_period`` so they can never disagree.
    """

    quota_type: str
    scope: str  # "group" | "individual" | "pool"
    scope_label: str
    limit_minutes: int
    users: tuple = ()
    equipment_ids: tuple = ()
    snapshot_filter: Optional[dict] = field(default=None, compare=False, hash=False)

    @property
    def period_word(self) -> str:
        return "Monthly" if self.quota_type == QuotaType.MONTHLY else "Weekly"

    def bookings_in_period(self, start_date: datetime, end_date: datetime, exclude_booking_id: Optional[int] = None) -> QuerySet:
        if self.scope == "pool":
            return QuotaService._legacy_bookings_in_period(
                equipment=self.equipment_ids[0],
                snapshot_filter=self.snapshot_filter or {},
                start_date=start_date,
                end_date=end_date,
                exclude_booking_id=exclude_booking_id,
            )
        return QuotaService._bookings_in_period(
            users=list(self.users),
            group_equipment_ids=list(self.equipment_ids),
            start_date=start_date,
            end_date=end_date,
            exclude_booking_id=exclude_booking_id,
        )


def remaining_slot_minutes_for_booking(booking) -> int:
    """Wall-clock minutes across slots still attached to the booking."""
    total = 0
    for slot in booking.daily_slots.all():
        start = getattr(slot, "start_datetime", None)
        end = getattr(slot, "end_datetime", None)
        if start and end:
            total += int((end - start).total_seconds() / 60)
    return max(0, total)


def booking_effective_quota_minutes(booking) -> int:
    """
    Minutes that count toward weekly/monthly/faculty quota for an active booking.

    Uses the stored booking time, capped by remaining slot duration so partial
    cancellation (released slots) frees quota immediately on the next booking attempt.
    """
    stored = max(0, int(getattr(booking, "total_time_minutes", None) or 0))
    slot_mins = remaining_slot_minutes_for_booking(booking)
    if slot_mins <= 0:
        return stored
    return min(stored, slot_mins)


def booking_effective_quota_charge(booking) -> Decimal:
    """Charge amount that counts toward CHARGE-type quota limits."""
    charge = Decimal(str(getattr(booking, "total_charge", None) or "0"))
    stored_mins = max(0, int(getattr(booking, "total_time_minutes", None) or 0))
    effective_mins = booking_effective_quota_minutes(booking)
    if stored_mins > 0 and effective_mins < stored_mins:
        return (charge * Decimal(effective_mins) / Decimal(stored_mins)).quantize(Decimal("0.01"))
    return charge.quantize(Decimal("0.01"))


def sync_booking_quota_fields_after_partial_cancel(
    booking,
    *,
    planned_minutes: int | None = None,
    planned_charge: Decimal | None = None,
) -> None:
    """
    Persist quota-relevant fields after partial cancellation.

    Ensures total_time_minutes / total_charge reflect only the remaining booking.
    """
    if planned_minutes is not None:
        booking.total_time_minutes = max(0, int(planned_minutes))
    if planned_charge is not None:
        booking.total_charge = max(Decimal("0.00"), planned_charge)

    before_cap = max(0, int(booking.total_time_minutes or 0))
    slot_mins = remaining_slot_minutes_for_booking(booking)
    after_cap = before_cap
    if slot_mins > 0:
        after_cap = min(before_cap, slot_mins)
    booking.total_time_minutes = after_cap

    if before_cap > 0 and after_cap < before_cap:
        charge = Decimal(str(booking.total_charge or "0"))
        booking.total_charge = (
            charge * Decimal(after_cap) / Decimal(before_cap)
        ).quantize(Decimal("0.01"))


def booking_quota_should_skip(equipment) -> bool:
    """True when quota checks should be skipped (global setting or per-equipment admin flag)."""
    from django.conf import settings

    if getattr(settings, "SKIP_BOOKING_QUOTA_CHECK", False):
        return True
    if equipment is not None and getattr(equipment, "skip_quota_check", False):
        return True
    return False


def booking_first_slot_start(booking):
    return (
        DailySlot.objects.filter(booking_id=booking.pk)
        .order_by("start_datetime")
        .values_list("start_datetime", flat=True)
        .first()
    )


def booking_quota_reference_datetime(booking):
    """
    The instant that decides which week / month a booking counts in: its quota anchor when set
    (disruption or staff moves keep the original period), otherwise its first slot start.
    A booking counts in exactly one week and one month, even when its slots cross a boundary.
    """
    return getattr(booking, "quota_period_anchor_at", None) or booking_first_slot_start(booking)


def booking_is_quota_exempt(booking) -> bool:
    """Repeat samples never consume quota (they re-run an already counted booking)."""
    return getattr(booking, "source_booking_id", None) is not None


def booking_counts_toward_quota(booking) -> bool:
    return not booking_is_quota_exempt(booking) and booking.status in QUOTA_COUNTING_STATUSES


def keep_quota_in_original_period(booking) -> None:
    """
    Before a disruption or staff reschedule moves the slots: pin the booking to the period it
    currently counts in, so the move neither frees the old period nor uses the new one.
    Call while the booking still holds its old slots; the caller saves the booking.
    """
    if getattr(booking, "quota_period_anchor_at", None) is None:
        booking.quota_period_anchor_at = booking_first_slot_start(booking)


def check_booking_minutes_change(booking, new_total_time_minutes: int) -> tuple[bool, Optional[str]]:
    """
    Quota check for an existing booking whose analysis minutes change (e.g. edited inputs).
    Only increases are checked, against the period the booking counts in; decreases always pass
    and free the difference because usage is derived from the stored minutes.
    """
    return evaluate_booking_minutes_change(booking, new_total_time_minutes).as_tuple()


def evaluate_booking_minutes_change(booking, new_total_time_minutes: int) -> "QuotaDecision":
    """check_booking_minutes_change with the failing limit attached."""
    if not booking_counts_toward_quota(booking) or booking_quota_should_skip(booking.equipment):
        return QUOTA_ALLOWED
    slot_mins = remaining_slot_minutes_for_booking(booking)
    old_effective = booking_effective_quota_minutes(booking)
    new_effective = max(0, int(new_total_time_minutes or 0))
    if slot_mins > 0:
        new_effective = min(new_effective, slot_mins)
    if new_effective <= old_effective:
        return QUOTA_ALLOWED
    decision = QuotaService.evaluate_booking_quota(
        booking.user,
        booking.equipment,
        additional_time_minutes=new_effective,
        additional_bookings=1,
        additional_charge=Decimal(str(booking.total_charge or "0")),
        booking_date=booking_quota_reference_datetime(booking),
        exclude_booking_id=booking.pk,
    )
    if decision.allowed:
        return QUOTA_ALLOWED
    return QuotaDecision(
        False,
        (
            f"This change needs {new_effective - old_effective} more minute(s) of instrument time "
            f"({old_effective} → {new_effective} min), which is over your booking limit. {decision.error}"
        ),
        decision.failure,
    )


# A week has 10,080 minutes and a 31-day month 44,640. Limits at ~90% of that (e.g. 10,075/week) can't
# realistically be used up by one user, so they mean "no limit": still enforced, but never shown as a quota.
EFFECTIVELY_UNLIMITED_QUOTA_MINUTES = {
    QuotaType.WEEKLY: 9000,
    QuotaType.MONTHLY: 40000,
}


def quota_limit_is_effectively_unlimited(quota_type: str, limit_minutes) -> bool:
    threshold = EFFECTIVELY_UNLIMITED_QUOTA_MINUTES.get(str(quota_type or "").upper())
    try:
        return threshold is not None and int(limit_minutes or 0) >= threshold
    except (TypeError, ValueError):
        return False


class QuotaService:
    """
    Reusable quota engine for equipment bookings.

    Evaluation order for internal users (group quotas):
      1. Faculty Monthly
      2. Faculty Weekly
      3. Individual Monthly (students only)
      4. Individual Weekly (students only)

    Faculty users stop after faculty checks.
    Urgent / hold bookings may bypass via bypass_quota=True.
    """

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def validate_booking_quota(
        cls,
        user: User,
        equipment,
        *,
        additional_time_minutes: int = 0,
        additional_bookings: int = 0,
        additional_charge: Decimal = Decimal("0.00"),
        booking_date: Optional[datetime] = None,
        exclude_booking_id: Optional[int] = None,
        bypass_quota: bool = False,
    ) -> tuple[bool, Optional[str]]:
        """
        Run the full quota pipeline for a booking request.

        Returns (allowed, error_message). When bypass_quota is True (urgent /
        hold flows), returns (True, None) immediately after skip checks.
        """
        return cls.evaluate_booking_quota(
            user,
            equipment,
            additional_time_minutes=additional_time_minutes,
            additional_bookings=additional_bookings,
            additional_charge=additional_charge,
            booking_date=booking_date,
            exclude_booking_id=exclude_booking_id,
            bypass_quota=bypass_quota,
        ).as_tuple()

    @classmethod
    def evaluate_booking_quota(
        cls,
        user: User,
        equipment,
        *,
        additional_time_minutes: int = 0,
        additional_bookings: int = 0,
        additional_charge: Decimal = Decimal("0.00"),
        booking_date: Optional[datetime] = None,
        exclude_booking_id: Optional[int] = None,
        bypass_quota: bool = False,
    ) -> QuotaDecision:
        """validate_booking_quota with the failing limit attached (scope, period, limit, used, requested)."""
        if bypass_quota or booking_quota_should_skip(equipment):
            return QUOTA_ALLOWED

        if booking_date is None:
            booking_date = timezone.now()

        equipment.refresh_from_db(fields=["equipment_group"])

        with transaction.atomic():
            if equipment.equipment_group_id:
                # Lock quota rows to serialize concurrent booking attempts for this group.
                list(
                    EquipmentGroupQuota.objects.select_for_update()
                    .filter(equipment_group_id=equipment.equipment_group_id, is_enforced=True)
                    .order_by("quota_type")
                )
                return cls._group_decision(
                    user=user,
                    equipment=equipment,
                    quota_types=(QuotaType.MONTHLY, QuotaType.WEEKLY),
                    additional_time_minutes=additional_time_minutes,
                    booking_date=booking_date,
                    exclude_booking_id=exclude_booking_id,
                )

            # Legacy equipment-level quotas (MONTHLY then WEEKLY for each configured limit).
            for quota_type in (QuotaType.MONTHLY, QuotaType.WEEKLY):
                decision = cls._legacy_decision(
                    user,
                    equipment,
                    quota_type,
                    additional_time_minutes,
                    additional_bookings,
                    additional_charge,
                    booking_date,
                    exclude_booking_id,
                )
                if not decision.allowed:
                    return decision
            return QUOTA_ALLOWED

    @classmethod
    def check_user_quota(
        cls,
        user: User,
        equipment,
        quota_type: str,
        additional_time_minutes: int = 0,
        additional_bookings: int = 0,
        additional_charge: Decimal = Decimal("0.00"),
        booking_date: Optional[datetime] = None,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Check a single period (WEEKLY or MONTHLY).

        Prefer validate_booking_quota() for new code so Faculty Monthly → …
        order is applied. This method remains for probes and legacy callers.
        """
        if booking_date is None:
            booking_date = timezone.now()

        equipment.refresh_from_db(fields=["equipment_group"])

        if equipment.equipment_group:
            # Single-period group check: faculty then individual within that period.
            return cls._group_decision(
                user=user,
                equipment=equipment,
                quota_types=(quota_type,),
                additional_time_minutes=additional_time_minutes,
                booking_date=booking_date,
                exclude_booking_id=exclude_booking_id,
            ).as_tuple()

        return cls._legacy_decision(
            user,
            equipment,
            quota_type,
            additional_time_minutes,
            additional_bookings,
            additional_charge,
            booking_date,
            exclude_booking_id,
        ).as_tuple()

    # ------------------------------------------------------------------
    # Group-level pipeline
    # ------------------------------------------------------------------

    @staticmethod
    def faculty_wallet_owner(user: User) -> Optional[User]:
        """The faculty whose wallet ``user`` books on (the faculty themselves for faculty users), else None."""
        if user.is_faculty():
            return user
        wallet = user.get_accessible_wallet()
        if wallet and wallet.user_id != user.pk and wallet.user.user_type == UserType.FACULTY:
            return wallet.user
        return None

    @classmethod
    def group_quota_dimensions(
        cls,
        user: User,
        equipment_group,
        quota_types=(QuotaType.MONTHLY, QuotaType.WEEKLY),
    ) -> list[QuotaDimension]:
        """
        Group limits that apply to ``user``, in enforcement order:
          1. Faculty Monthly / Weekly (faculty, or a student on a faculty wallet): shared by the wallet group
          2. Individual Monthly / Weekly (everyone except faculty)
        A limit of 0 means "no limit" and is still listed (callers skip it).
        """
        group_equipment_ids = tuple(equipment_group.equipment.values_list("equipment_id", flat=True))
        is_internal = UserType.is_internal_user(user.user_type)
        is_faculty = bool(user.is_faculty())
        use_faculty_quota = cls.faculty_wallet_owner(user) is not None
        quotas = {
            q.quota_type: q
            for q in EquipmentGroupQuota.objects.filter(
                equipment_group=equipment_group, quota_type__in=list(quota_types), is_enforced=True
            )
        }
        ordered = [quotas[t] for t in quota_types if t in quotas]
        dims: list[QuotaDimension] = []
        if use_faculty_quota and ordered:
            members = tuple(cls._wallet_users(user))
            for q in ordered:
                period_word = "Monthly" if q.quota_type == QuotaType.MONTHLY else "Weekly"
                dims.append(
                    QuotaDimension(
                        quota_type=q.quota_type,
                        scope="group",
                        scope_label=f"Faculty {period_word}",
                        limit_minutes=int(
                            (q.internal_faculty_quota_minutes if is_internal else q.external_faculty_quota_minutes) or 0
                        ),
                        users=members,
                        equipment_ids=group_equipment_ids,
                    )
                )
        if not is_faculty:
            for q in ordered:
                period_word = "Monthly" if q.quota_type == QuotaType.MONTHLY else "Weekly"
                dims.append(
                    QuotaDimension(
                        quota_type=q.quota_type,
                        scope="individual",
                        scope_label=f"Individual {period_word}",
                        limit_minutes=int(
                            (q.internal_individual_quota_minutes if is_internal else q.external_individual_quota_minutes)
                            or 0
                        ),
                        users=(user,),
                        equipment_ids=group_equipment_ids,
                    )
                )
        return dims

    @classmethod
    def evaluate_dimension(
        cls,
        dim: QuotaDimension,
        *,
        booking_date: datetime,
        additional_time_minutes: int = 0,
        exclude_booking_id: Optional[int] = None,
    ) -> QuotaCheckResult:
        start_date, end_date = cls._get_quota_period(dim.quota_type, booking_date)
        common = dict(
            scope=dim.scope_label,
            requested_minutes=additional_time_minutes,
            quota_type=dim.quota_type,
            scope_kind=dim.scope,
            period_start=start_date,
            period_end=end_date,
            members_count=len(dim.users) or 1,
        )
        # Group limits of 0 mean "not configured"; a legacy HOURS limit of 0 blocks any booking.
        if dim.limit_minutes <= 0 and dim.scope != "pool":
            return QuotaCheckResult(allowed=True, used_minutes=0, limit_minutes=0, remaining_before_request=0, **common)
        used = cls._sum_booking_quota_minutes(dim.bookings_in_period(start_date, end_date, exclude_booking_id))
        return QuotaCheckResult(
            allowed=used + additional_time_minutes <= dim.limit_minutes,
            used_minutes=used,
            limit_minutes=dim.limit_minutes,
            remaining_before_request=max(0, dim.limit_minutes - used),
            **common,
        )

    @classmethod
    def _group_decision(
        cls,
        *,
        user: User,
        equipment,
        quota_types,
        additional_time_minutes: int,
        booking_date: datetime,
        exclude_booking_id: Optional[int],
    ) -> QuotaDecision:
        for dim in cls.group_quota_dimensions(user, equipment.equipment_group, quota_types):
            result = cls.evaluate_dimension(
                dim,
                booking_date=booking_date,
                additional_time_minutes=additional_time_minutes,
                exclude_booking_id=exclude_booking_id,
            )
            if not result.allowed:
                return QuotaDecision(False, result.as_error(), result)
        return QUOTA_ALLOWED

    # ------------------------------------------------------------------
    # Usage queries
    # ------------------------------------------------------------------

    @classmethod
    def _base_quota_bookings_qs(cls) -> QuerySet:
        """Bookings that consume quota (excludes repeat samples and non-consuming statuses)."""
        return Booking.objects.filter(
            status__in=QUOTA_COUNTING_STATUSES,
            source_booking__isnull=True,  # exclude Repeat Sample
        )

    @staticmethod
    def _in_quota_period(qs: QuerySet, start_date: datetime, end_date: datetime) -> QuerySet:
        """Keep bookings whose quota reference (anchor, else first slot start) is in the period."""
        first_slot_start = (
            DailySlot.objects.filter(booking_id=OuterRef("pk"))
            .order_by("start_datetime")
            .values("start_datetime")[:1]
        )
        return qs.annotate(
            quota_reference_at=Coalesce("quota_period_anchor_at", Subquery(first_slot_start))
        ).filter(quota_reference_at__gte=start_date, quota_reference_at__lte=end_date)

    @classmethod
    def _bookings_in_period(
        cls,
        *,
        users,
        group_equipment_ids,
        start_date: datetime,
        end_date: datetime,
        exclude_booking_id: Optional[int],
    ) -> QuerySet:
        qs = cls._in_quota_period(
            cls._base_quota_bookings_qs().filter(
                user__in=users,
                equipment_id__in=group_equipment_ids,
            ),
            start_date,
            end_date,
        )
        if exclude_booking_id is not None:
            qs = qs.exclude(booking_id=exclude_booking_id)
        return qs

    @classmethod
    def _sum_booking_quota_minutes(cls, bookings_qs) -> int:
        bookings = list(bookings_qs.prefetch_related("daily_slots"))
        return sum(booking_effective_quota_minutes(b) for b in bookings)

    @classmethod
    def _sum_booking_quota_charge(cls, bookings_qs) -> Decimal:
        bookings = list(bookings_qs.prefetch_related("daily_slots"))
        total = Decimal("0.00")
        for b in bookings:
            total += booking_effective_quota_charge(b)
        return total.quantize(Decimal("0.01"))

    @classmethod
    def _wallet_users(cls, user: User) -> list:
        wallet = user.get_accessible_wallet()
        if not wallet:
            return [user]
        users = [wallet.user]
        approved = WalletJoinRequest.objects.filter(
            wallet=wallet,
            status=WalletJoinRequestStatus.APPROVED,
        ).select_related("student")
        users.extend([req.student for req in approved if req.student_id])
        # Deduplicate while preserving order
        seen = set()
        unique = []
        for u in users:
            if u is None or u.pk in seen:
                continue
            seen.add(u.pk)
            unique.append(u)
        return unique

    # ------------------------------------------------------------------
    # Legacy equipment-level quotas
    # ------------------------------------------------------------------

    LEGACY_EXTERNAL_SNAPSHOT_FILTER = {"user_type_snapshot__in": ["external", "EXTERNAL"]}

    @classmethod
    def _legacy_bookings_in_period(
        cls,
        *,
        equipment,
        snapshot_filter: dict,
        start_date: datetime,
        end_date: datetime,
        exclude_booking_id: Optional[int] = None,
    ) -> QuerySet:
        """Quota-consuming bookings on one equipment for a user-type snapshot within a period."""
        qs = cls._in_quota_period(
            cls._base_quota_bookings_qs().filter(equipment=equipment, **snapshot_filter),
            start_date,
            end_date,
        )
        if exclude_booking_id is not None:
            qs = qs.exclude(booking_id=exclude_booking_id)
        return qs

    @classmethod
    def _legacy_quotas(cls, user: User, equipment, quota_type: str):
        """(quotas, snapshot_filter, label prefix) of the equipment-level limits that apply to ``user``."""
        if user.is_external():
            quotas = ExternalUserQuota.objects.filter(equipment=equipment, quota_type=quota_type, is_enforced=True)
            return list(quotas), cls.LEGACY_EXTERNAL_SNAPSHOT_FILTER, "External"
        quotas = UserTypeQuota.objects.filter(
            equipment=equipment, user_type=user.user_type, quota_type=quota_type, is_enforced=True
        )
        return list(quotas), {"user_type_snapshot": user.user_type}, "Individual"

    @classmethod
    def legacy_quota_dimensions(
        cls, user: User, equipment, quota_types=(QuotaType.MONTHLY, QuotaType.WEEKLY)
    ) -> list[QuotaDimension]:
        """Equipment-level minute (HOURS) limits: shared by every booking of the user's type on the equipment."""
        dims: list[QuotaDimension] = []
        for quota_type in quota_types:
            quotas, snapshot_filter, prefix = cls._legacy_quotas(user, equipment, quota_type)
            for quota in quotas:
                if quota.limit_type != QuotaLimitType.HOURS:
                    continue
                dims.append(cls._legacy_dimension(quota, equipment, snapshot_filter, prefix))
        return dims

    @staticmethod
    def _legacy_dimension(quota, equipment, snapshot_filter: dict, prefix: str) -> QuotaDimension:
        period_word = "Monthly" if quota.quota_type == QuotaType.MONTHLY else "Weekly"
        return QuotaDimension(
            quota_type=quota.quota_type,
            scope="pool",
            scope_label=f"{prefix} {period_word}",
            limit_minutes=int(quota.limit_value or 0),
            equipment_ids=(equipment.pk,),
            snapshot_filter=snapshot_filter,
        )

    @classmethod
    def _legacy_decision(
        cls,
        user: User,
        equipment,
        quota_type: str,
        additional_time_minutes: int,
        additional_bookings: int,
        additional_charge: Decimal,
        booking_date: datetime,
        exclude_booking_id: Optional[int] = None,
    ) -> QuotaDecision:
        quotas, snapshot_filter, prefix = cls._legacy_quotas(user, equipment, quota_type)
        if not quotas:
            return QUOTA_ALLOWED

        start_date, end_date = cls._get_quota_period(quota_type, booking_date)
        existing_bookings = cls._legacy_bookings_in_period(
            equipment=equipment,
            snapshot_filter=snapshot_filter,
            start_date=start_date,
            end_date=end_date,
            exclude_booking_id=exclude_booking_id,
        )

        period_label = "Monthly" if quota_type == QuotaType.MONTHLY else "Weekly"
        for quota in quotas:
            if quota.limit_type == QuotaLimitType.HOURS:
                result = cls.evaluate_dimension(
                    cls._legacy_dimension(quota, equipment, snapshot_filter, prefix),
                    booking_date=booking_date,
                    additional_time_minutes=additional_time_minutes,
                    exclude_booking_id=exclude_booking_id,
                )
                if not result.allowed:
                    return QuotaDecision(False, result.as_error(), result)
            elif quota.limit_type == QuotaLimitType.BOOKINGS:
                total_bookings = existing_bookings.count() + additional_bookings
                if total_bookings > quota.limit_value:
                    return QuotaDecision(False, (
                        f"{prefix} {period_label} booking-count quota exceeded: "
                        f"{total_bookings} bookings vs limit {quota.limit_value}."
                    ))
            elif quota.limit_type == QuotaLimitType.CHARGE:
                used_charge = cls._sum_booking_quota_charge(existing_bookings)
                projected = used_charge + additional_charge
                if projected > quota.limit_value:
                    return QuotaDecision(False, (
                        f"{prefix} {period_label} charge quota exceeded: "
                        f"₹{projected} vs limit ₹{quota.limit_value}."
                    ))
        return QUOTA_ALLOWED

    # ------------------------------------------------------------------
    # Period boundaries (Monday–Sunday week; calendar month)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_quota_period(
        quota_type: str, reference_date: Optional[datetime] = None
    ) -> tuple[datetime, datetime]:
        """
        Return (start, end) for WEEKLY (Mon 00:00 – Sun 23:59:59.999999 local)
        or MONTHLY (1st 00:00 – last day 23:59:59.999999 local).
        """
        if reference_date is None:
            reference_date = timezone.now()

        if timezone.is_naive(reference_date):
            reference_date = timezone.make_aware(
                reference_date, timezone.get_current_timezone()
            )
        reference_date = timezone.localtime(reference_date)

        if quota_type == QuotaType.WEEKLY:
            # Monday = 0 … Sunday = 6
            days_since_monday = reference_date.weekday()
            start_date = reference_date - timedelta(days=days_since_monday)
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
            sunday_date = start_date + timedelta(days=6)
            end_date = sunday_date.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif quota_type == QuotaType.MONTHLY:
            start_date = reference_date.replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            if start_date.month == 12:
                next_month_start = start_date.replace(year=start_date.year + 1, month=1)
            else:
                next_month_start = start_date.replace(month=start_date.month + 1)
            last_day = next_month_start - timedelta(days=1)
            end_date = last_day.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            raise ValueError(f"Invalid quota type: {quota_type}")

        return start_date, end_date


# Backward-compatible alias used across the codebase.
class QuotaChecker(QuotaService):
    """Alias for QuotaService (legacy name)."""

    pass


def get_quota_breakdown(user, equipment, quota_type: str, reference_date: datetime, failure_reason: str = ""):
    """
    Date-wise quota usage in the older attempt-log shape (period, limit, total, events), built from the
    same breakdown as /bookings/quota-breakdown/.
    """
    from .quota_breakdown import BreakdownError, build_quota_breakdown, resolve_dimension, scope_from_failure_reason

    start_date, end_date = QuotaService._get_quota_period(quota_type, reference_date)
    equipment.refresh_from_db(fields=["equipment_group"])
    empty = {
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "quota_type": quota_type,
        "quota_scope": "group" if equipment.equipment_group_id else "individual",
        "limit_minutes": 0,
        "total_minutes": 0,
        "summary_message": "No quota configured for this limit.",
        "events": [],
    }
    scope = scope_from_failure_reason(failure_reason, equipment) if failure_reason else None
    try:
        dim = resolve_dimension(user, equipment, quota_type, scope)
    except BreakdownError:
        return empty
    data = build_quota_breakdown(user, equipment, dim, reference_date)
    quota_scope = {"group": "faculty", "individual": "individual"}.get(dim.scope) or (
        "external" if user.is_external() else "user_type"
    )
    events = [
        {
            "date": timezone.localtime(datetime.fromisoformat(r["slot_start"])).strftime("%Y-%m-%d") if r["slot_start"] else "",
            "booking_id": r["display_booking_id"],
            "real_booking_id": r["booking_id"],
            "equipment_name": r["equipment_name"],
            "equipment_code": r["equipment_code"],
            "display_booking_id": r["display_booking_id"],
            "total_time_minutes": r["minutes"],
            "user_name": r["user_name"],
        }
        for r in data["counted"]
    ]
    events.sort(key=lambda e: (e["date"], e["real_booking_id"]))
    return {
        **empty,
        "quota_scope": quota_scope,
        "limit_minutes": data["limit_minutes"],
        "total_minutes": data["used_minutes"],
        "summary_message": (
            f"{data['used_minutes']} minutes used out of {data['limit_minutes']} minutes limit "
            f"({quota_type.lower()}, {quota_scope})"
        ),
        "events": events,
    }

