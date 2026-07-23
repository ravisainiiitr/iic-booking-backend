"""
Equipment quota management for internal (and external) bookings.

QuotaService is the single entry point for quota validation and usage
aggregation. QuotaChecker remains as a thin alias for older call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional

from django.db import transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

from .models import (
    Booking,
    BookingStatus,
    EquipmentGroupQuota,
    ExternalUserQuota,
    QuotaLimitType,
    QuotaType,
    UserTypeQuota,
)

# Only bookings that represent actual quota consumption.
# Cancelled / refunded / disruption / hold never count.
QUOTA_COUNTING_STATUSES = (
    BookingStatus.BOOKED,
    BookingStatus.COMPLETED,
)

QUOTA_EXCLUDED_STATUSES = (
    BookingStatus.CANCELLED,
    BookingStatus.REFUNDED,
    BookingStatus.DISRUPTION_PENDING,
    BookingStatus.UNDER_MAINTENANCE,
    BookingStatus.OTHER_DISRUPTION,
    BookingStatus.HOLD,
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

    def as_error(self) -> str:
        if self.message:
            return self.message
        projected = self.used_minutes + self.requested_minutes
        return (
            f"{self.scope} quota exceeded: "
            f"current usage {self.used_minutes} min + requested {self.requested_minutes} min "
            f"= {projected} min; configured limit {self.limit_minutes} min; "
            f"remaining before this request {max(0, self.remaining_before_request)} min."
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
        if bypass_quota or booking_quota_should_skip(equipment):
            return True, None

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
                return cls._validate_group_quotas(
                    user=user,
                    equipment=equipment,
                    additional_time_minutes=additional_time_minutes,
                    booking_date=booking_date,
                    exclude_booking_id=exclude_booking_id,
                )

            # Legacy equipment-level quotas (WEEKLY then MONTHLY for each configured limit).
            for quota_type in (QuotaType.MONTHLY, QuotaType.WEEKLY):
                ok, err = cls.check_user_quota(
                    user=user,
                    equipment=equipment,
                    quota_type=quota_type,
                    additional_time_minutes=additional_time_minutes,
                    additional_bookings=additional_bookings,
                    additional_charge=additional_charge,
                    booking_date=booking_date,
                    exclude_booking_id=exclude_booking_id,
                )
                if not ok:
                    return False, err
            return True, None

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
            return cls._check_group_quota_period(
                user,
                equipment,
                quota_type,
                additional_time_minutes,
                booking_date,
                exclude_booking_id,
            )

        if user.is_external():
            return cls._check_external_quota(
                equipment,
                quota_type,
                additional_time_minutes,
                additional_bookings,
                additional_charge,
                booking_date,
                exclude_booking_id,
            )
        return cls._check_user_type_quota(
            equipment,
            user.user_type,
            quota_type,
            additional_time_minutes,
            additional_bookings,
            additional_charge,
            booking_date,
            exclude_booking_id,
        )

    # ------------------------------------------------------------------
    # Group-level pipeline
    # ------------------------------------------------------------------

    @classmethod
    def _validate_group_quotas(
        cls,
        *,
        user: User,
        equipment,
        additional_time_minutes: int,
        booking_date: datetime,
        exclude_booking_id: Optional[int],
    ) -> tuple[bool, Optional[str]]:
        equipment_group = equipment.equipment_group
        group_equipment_ids = list(
            equipment_group.equipment.values_list("equipment_id", flat=True)
        )
        is_internal = UserType.is_internal_user(user.user_type)
        is_faculty = bool(user.is_faculty())
        wallet = user.get_accessible_wallet()
        is_using_faculty_wallet = bool(
            wallet
            and wallet.user.user_type == UserType.FACULTY
            and wallet.user_id != user.pk
        )
        use_faculty_quota = is_faculty or is_using_faculty_wallet

        monthly = cls._get_group_quota(equipment_group, QuotaType.MONTHLY)
        weekly = cls._get_group_quota(equipment_group, QuotaType.WEEKLY)

        # Steps 1–2: Faculty Monthly then Faculty Weekly
        if use_faculty_quota:
            for quota_obj, period_label in (
                (monthly, "Faculty Monthly"),
                (weekly, "Faculty Weekly"),
            ):
                if quota_obj is None:
                    continue
                limit = (
                    quota_obj.internal_faculty_quota_minutes
                    if is_internal
                    else quota_obj.external_faculty_quota_minutes
                )
                result = cls._evaluate_faculty_minutes(
                    user=user,
                    group_equipment_ids=group_equipment_ids,
                    limit_minutes=limit,
                    quota_type=quota_obj.quota_type,
                    booking_date=booking_date,
                    additional_time_minutes=additional_time_minutes,
                    scope_label=period_label,
                    exclude_booking_id=exclude_booking_id,
                )
                if not result.allowed:
                    return False, result.as_error()

            # Faculty users: no individual checks.
            if is_faculty:
                return True, None

        # Steps 3–4: Individual Monthly then Individual Weekly (students / non-faculty)
        if not is_faculty:
            for quota_obj, period_label in (
                (monthly, "Individual Monthly"),
                (weekly, "Individual Weekly"),
            ):
                if quota_obj is None:
                    continue
                limit = (
                    quota_obj.internal_individual_quota_minutes
                    if is_internal
                    else quota_obj.external_individual_quota_minutes
                )
                result = cls._evaluate_individual_minutes(
                    user=user,
                    group_equipment_ids=group_equipment_ids,
                    limit_minutes=limit,
                    quota_type=quota_obj.quota_type,
                    booking_date=booking_date,
                    additional_time_minutes=additional_time_minutes,
                    scope_label=period_label,
                    exclude_booking_id=exclude_booking_id,
                )
                if not result.allowed:
                    return False, result.as_error()

        return True, None

    @classmethod
    def _check_group_quota_period(
        cls,
        user: User,
        equipment,
        quota_type: str,
        additional_time_minutes: int,
        booking_date: datetime,
        exclude_booking_id: Optional[int],
    ) -> tuple[bool, Optional[str]]:
        """Single-period group check (legacy API): faculty then individual within that period."""
        equipment_group = equipment.equipment_group
        group_quota = cls._get_group_quota(equipment_group, quota_type)
        if group_quota is None:
            return True, None

        group_equipment_ids = list(
            equipment_group.equipment.values_list("equipment_id", flat=True)
        )
        is_internal = UserType.is_internal_user(user.user_type)
        is_faculty = bool(user.is_faculty())
        wallet = user.get_accessible_wallet()
        is_using_faculty_wallet = bool(
            wallet
            and wallet.user.user_type == UserType.FACULTY
            and wallet.user_id != user.pk
        )
        use_faculty_quota = is_faculty or is_using_faculty_wallet
        period_name = "Monthly" if quota_type == QuotaType.MONTHLY else "Weekly"

        if use_faculty_quota:
            limit = (
                group_quota.internal_faculty_quota_minutes
                if is_internal
                else group_quota.external_faculty_quota_minutes
            )
            result = cls._evaluate_faculty_minutes(
                user=user,
                group_equipment_ids=group_equipment_ids,
                limit_minutes=limit,
                quota_type=quota_type,
                booking_date=booking_date,
                additional_time_minutes=additional_time_minutes,
                scope_label=f"Faculty {period_name}",
                exclude_booking_id=exclude_booking_id,
            )
            if not result.allowed:
                return False, result.as_error()
            if is_faculty:
                return True, None

        if not is_faculty:
            limit = (
                group_quota.internal_individual_quota_minutes
                if is_internal
                else group_quota.external_individual_quota_minutes
            )
            result = cls._evaluate_individual_minutes(
                user=user,
                group_equipment_ids=group_equipment_ids,
                limit_minutes=limit,
                quota_type=quota_type,
                booking_date=booking_date,
                additional_time_minutes=additional_time_minutes,
                scope_label=f"Individual {period_name}",
                exclude_booking_id=exclude_booking_id,
            )
            if not result.allowed:
                return False, result.as_error()

        return True, None

    @staticmethod
    def _get_group_quota(equipment_group, quota_type: str) -> Optional[EquipmentGroupQuota]:
        try:
            return EquipmentGroupQuota.objects.get(
                equipment_group=equipment_group,
                quota_type=quota_type,
                is_enforced=True,
            )
        except EquipmentGroupQuota.DoesNotExist:
            return None

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
        bookings_with_slots_in_period = Booking.objects.filter(
            pk=OuterRef("pk"),
            daily_slots__start_datetime__gte=start_date,
            daily_slots__start_datetime__lte=end_date,
        )
        qs = (
            cls._base_quota_bookings_qs()
            .filter(
                user__in=users,
                equipment_id__in=group_equipment_ids,
            )
            .filter(
                Q(
                    quota_period_anchor_at__isnull=False,
                    quota_period_anchor_at__gte=start_date,
                    quota_period_anchor_at__lte=end_date,
                )
                | (Q(quota_period_anchor_at__isnull=True) & Exists(bookings_with_slots_in_period))
            )
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

    @classmethod
    def _evaluate_individual_minutes(
        cls,
        *,
        user: User,
        group_equipment_ids,
        limit_minutes: int,
        quota_type: str,
        booking_date: datetime,
        additional_time_minutes: int,
        scope_label: str,
        exclude_booking_id: Optional[int],
    ) -> QuotaCheckResult:
        if limit_minutes <= 0:
            return QuotaCheckResult(
                allowed=True,
                scope=scope_label,
                used_minutes=0,
                requested_minutes=additional_time_minutes,
                limit_minutes=0,
                remaining_before_request=0,
            )
        start_date, end_date = cls._get_quota_period(quota_type, booking_date)
        existing = cls._bookings_in_period(
            users=[user],
            group_equipment_ids=group_equipment_ids,
            start_date=start_date,
            end_date=end_date,
            exclude_booking_id=exclude_booking_id,
        )
        used = cls._sum_booking_quota_minutes(existing)
        remaining = max(0, limit_minutes - used)
        projected = used + additional_time_minutes
        allowed = projected <= limit_minutes
        return QuotaCheckResult(
            allowed=allowed,
            scope=scope_label,
            used_minutes=used,
            requested_minutes=additional_time_minutes,
            limit_minutes=limit_minutes,
            remaining_before_request=remaining,
        )

    @classmethod
    def _evaluate_faculty_minutes(
        cls,
        *,
        user: User,
        group_equipment_ids,
        limit_minutes: int,
        quota_type: str,
        booking_date: datetime,
        additional_time_minutes: int,
        scope_label: str,
        exclude_booking_id: Optional[int],
    ) -> QuotaCheckResult:
        if limit_minutes <= 0:
            return QuotaCheckResult(
                allowed=True,
                scope=scope_label,
                used_minutes=0,
                requested_minutes=additional_time_minutes,
                limit_minutes=0,
                remaining_before_request=0,
            )
        wallet_users = cls._wallet_users(user)
        start_date, end_date = cls._get_quota_period(quota_type, booking_date)
        existing = cls._bookings_in_period(
            users=wallet_users,
            group_equipment_ids=group_equipment_ids,
            start_date=start_date,
            end_date=end_date,
            exclude_booking_id=exclude_booking_id,
        )
        used = cls._sum_booking_quota_minutes(existing)
        remaining = max(0, limit_minutes - used)
        projected = used + additional_time_minutes
        allowed = projected <= limit_minutes
        msg = None
        if not allowed:
            msg = (
                f"{scope_label} quota exceeded: "
                f"current usage {used} min + requested {additional_time_minutes} min "
                f"= {projected} min; configured limit {limit_minutes} min; "
                f"remaining before this request {remaining} min "
                f"(shared across {len(wallet_users)} user(s) on the faculty wallet)."
            )
        return QuotaCheckResult(
            allowed=allowed,
            scope=scope_label,
            used_minutes=used,
            requested_minutes=additional_time_minutes,
            limit_minutes=limit_minutes,
            remaining_before_request=remaining,
            message=msg,
        )

    # ------------------------------------------------------------------
    # Legacy equipment-level quotas
    # ------------------------------------------------------------------

    @classmethod
    def _check_user_type_quota(
        cls,
        equipment,
        user_type: str,
        quota_type: str,
        additional_time_minutes: int,
        additional_bookings: int,
        additional_charge: Decimal,
        booking_date: datetime,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        start_date, end_date = cls._get_quota_period(quota_type, booking_date)
        quotas = list(
            UserTypeQuota.objects.filter(
                equipment=equipment,
                user_type=user_type,
                quota_type=quota_type,
                is_enforced=True,
            )
        )
        if not quotas:
            return True, None

        bookings_with_slots_in_period = Booking.objects.filter(
            pk=OuterRef("pk"),
            daily_slots__start_datetime__gte=start_date,
            daily_slots__start_datetime__lte=end_date,
        )
        existing_bookings = (
            cls._base_quota_bookings_qs()
            .filter(
                equipment=equipment,
                user_type_snapshot=user_type,
            )
            .filter(
                Q(
                    quota_period_anchor_at__isnull=False,
                    quota_period_anchor_at__gte=start_date,
                    quota_period_anchor_at__lte=end_date,
                )
                | (Q(quota_period_anchor_at__isnull=True) & Exists(bookings_with_slots_in_period))
            )
        )
        if exclude_booking_id is not None:
            existing_bookings = existing_bookings.exclude(booking_id=exclude_booking_id)

        period_label = "Monthly" if quota_type == QuotaType.MONTHLY else "Weekly"
        for quota in quotas:
            if quota.limit_type == QuotaLimitType.HOURS:
                used = cls._sum_booking_quota_minutes(existing_bookings)
                projected = used + additional_time_minutes
                if projected > quota.limit_value:
                    remaining = max(0, int(quota.limit_value) - used)
                    return False, (
                        f"Individual {period_label} quota exceeded: "
                        f"current usage {used} min + requested {additional_time_minutes} min "
                        f"= {projected} min; configured limit {int(quota.limit_value)} min; "
                        f"remaining before this request {remaining} min."
                    )
            elif quota.limit_type == QuotaLimitType.BOOKINGS:
                total_bookings = existing_bookings.count() + additional_bookings
                if total_bookings > quota.limit_value:
                    return False, (
                        f"Individual {period_label} booking-count quota exceeded: "
                        f"{total_bookings} bookings vs limit {quota.limit_value}."
                    )
            elif quota.limit_type == QuotaLimitType.CHARGE:
                used_charge = cls._sum_booking_quota_charge(existing_bookings)
                projected = used_charge + additional_charge
                if projected > quota.limit_value:
                    return False, (
                        f"Individual {period_label} charge quota exceeded: "
                        f"₹{projected} vs limit ₹{quota.limit_value}."
                    )
        return True, None

    @classmethod
    def _check_external_quota(
        cls,
        equipment,
        quota_type: str,
        additional_time_minutes: int,
        additional_bookings: int,
        additional_charge: Decimal,
        booking_date: datetime,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        start_date, end_date = cls._get_quota_period(quota_type, booking_date)
        quotas = list(
            ExternalUserQuota.objects.filter(
                equipment=equipment,
                quota_type=quota_type,
                is_enforced=True,
            )
        )
        if not quotas:
            return True, None

        bookings_with_slots_in_period = Booking.objects.filter(
            pk=OuterRef("pk"),
            daily_slots__start_datetime__gte=start_date,
            daily_slots__start_datetime__lte=end_date,
        )
        existing_bookings = (
            cls._base_quota_bookings_qs()
            .filter(
                equipment=equipment,
                user_type_snapshot__in=["external", "EXTERNAL"],
            )
            .filter(
                Q(
                    quota_period_anchor_at__isnull=False,
                    quota_period_anchor_at__gte=start_date,
                    quota_period_anchor_at__lte=end_date,
                )
                | (Q(quota_period_anchor_at__isnull=True) & Exists(bookings_with_slots_in_period))
            )
        )
        if exclude_booking_id is not None:
            existing_bookings = existing_bookings.exclude(booking_id=exclude_booking_id)

        period_label = "Monthly" if quota_type == QuotaType.MONTHLY else "Weekly"
        for quota in quotas:
            if quota.limit_type == QuotaLimitType.HOURS:
                used = cls._sum_booking_quota_minutes(existing_bookings)
                projected = used + additional_time_minutes
                if projected > quota.limit_value:
                    remaining = max(0, int(quota.limit_value) - used)
                    return False, (
                        f"External {period_label} quota exceeded: "
                        f"current usage {used} min + requested {additional_time_minutes} min "
                        f"= {projected} min; configured limit {int(quota.limit_value)} min; "
                        f"remaining before this request {remaining} min."
                    )
            elif quota.limit_type == QuotaLimitType.BOOKINGS:
                total_bookings = existing_bookings.count() + additional_bookings
                if total_bookings > quota.limit_value:
                    return False, (
                        f"External {period_label} booking-count quota exceeded: "
                        f"{total_bookings} bookings vs limit {quota.limit_value}."
                    )
            elif quota.limit_type == QuotaLimitType.CHARGE:
                used_charge = cls._sum_booking_quota_charge(existing_bookings)
                projected = used_charge + additional_charge
                if projected > quota.limit_value:
                    return False, (
                        f"External {period_label} charge quota exceeded: "
                        f"₹{projected} vs limit ₹{quota.limit_value}."
                    )
        return True, None

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
    Return date-wise breakdown of quota usage for display (admin/OIC).
    Used when a booking attempt failed due to weekly/monthly quota.
    """
    start_date, end_date = QuotaService._get_quota_period(quota_type, reference_date)
    equipment.refresh_from_db(fields=["equipment_group"])
    events = []
    limit_minutes = 0
    total_minutes = 0
    quota_scope = "individual"

    bookings_with_slots_in_period = Booking.objects.filter(
        pk=OuterRef("pk"),
        daily_slots__start_datetime__gte=start_date,
        daily_slots__start_datetime__lte=end_date,
    )

    if equipment.equipment_group:
        try:
            group_quota = EquipmentGroupQuota.objects.get(
                equipment_group=equipment.equipment_group,
                quota_type=quota_type,
                is_enforced=True,
            )
        except EquipmentGroupQuota.DoesNotExist:
            return {
                "period_start": start_date.isoformat(),
                "period_end": end_date.isoformat(),
                "quota_type": quota_type,
                "quota_scope": "group",
                "limit_minutes": 0,
                "total_minutes": 0,
                "summary_message": "No quota configured for this group.",
                "events": [],
            }

        group_equipment_ids = list(
            equipment.equipment_group.equipment.values_list("equipment_id", flat=True)
        )
        is_internal = UserType.is_internal_user(user.user_type)
        use_faculty = "faculty" in (failure_reason or "").lower()

        if use_faculty:
            quota_scope = "faculty"
            wallet_users = QuotaService._wallet_users(user)
            limit_minutes = (
                group_quota.internal_faculty_quota_minutes
                if is_internal
                else group_quota.external_faculty_quota_minutes
            )
            existing_bookings = QuotaService._bookings_in_period(
                users=wallet_users,
                group_equipment_ids=group_equipment_ids,
                start_date=start_date,
                end_date=end_date,
                exclude_booking_id=None,
            )
        else:
            limit_minutes = (
                group_quota.internal_individual_quota_minutes
                if is_internal
                else group_quota.external_individual_quota_minutes
            )
            existing_bookings = QuotaService._bookings_in_period(
                users=[user],
                group_equipment_ids=group_equipment_ids,
                start_date=start_date,
                end_date=end_date,
                exclude_booking_id=None,
            )

        total_minutes = QuotaService._sum_booking_quota_minutes(existing_bookings)
        for b in existing_bookings.select_related("equipment", "user").order_by("booking_id"):
            slot_date = (
                b.daily_slots.filter(
                    start_datetime__gte=start_date,
                    start_datetime__lte=end_date,
                )
                .order_by("date")
                .values_list("date", flat=True)
                .first()
            )
            date_str = slot_date.strftime("%Y-%m-%d") if slot_date else ""
            events.append(
                {
                    "date": date_str,
                    "booking_id": (b.virtual_booking_id or "").strip()
                    or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                    "real_booking_id": b.booking_id,
                    "equipment_name": b.equipment.name if b.equipment else "",
                    "equipment_code": b.equipment.code if b.equipment else "",
                    "display_booking_id": (b.virtual_booking_id or "").strip()
                    or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                    "total_time_minutes": booking_effective_quota_minutes(b),
                    "user_name": (b.user.name or b.user.email) if b.user else "",
                }
            )
    else:
        if user.is_external():
            quotas = ExternalUserQuota.objects.filter(
                equipment=equipment,
                quota_type=quota_type,
                is_enforced=True,
            )
            existing_bookings = (
                QuotaService._base_quota_bookings_qs()
                .filter(
                    equipment=equipment,
                    user_type_snapshot__in=["external", "EXTERNAL"],
                )
                .filter(Exists(bookings_with_slots_in_period))
            )
            quota_scope = "external"
        else:
            user_type = user.user_type
            quotas = UserTypeQuota.objects.filter(
                equipment=equipment,
                user_type=user_type,
                quota_type=quota_type,
                is_enforced=True,
            )
            existing_bookings = (
                QuotaService._base_quota_bookings_qs()
                .filter(
                    equipment=equipment,
                    user_type_snapshot=user_type,
                )
                .filter(Exists(bookings_with_slots_in_period))
            )
            quota_scope = "user_type"

        for q in quotas:
            if getattr(q, "limit_type", None) == QuotaLimitType.HOURS:
                limit_minutes = int(q.limit_value) if q.limit_value is not None else 0
                break
            if hasattr(q, "limit_value") and q.limit_value is not None:
                limit_minutes = int(q.limit_value)
                break

        total_minutes = QuotaService._sum_booking_quota_minutes(existing_bookings)
        for b in existing_bookings.select_related("equipment", "user").order_by("booking_id"):
            slot_date = (
                b.daily_slots.filter(
                    start_datetime__gte=start_date,
                    start_datetime__lte=end_date,
                )
                .order_by("date")
                .values_list("date", flat=True)
                .first()
            )
            date_str = slot_date.strftime("%Y-%m-%d") if slot_date else ""
            events.append(
                {
                    "date": date_str,
                    "booking_id": (b.virtual_booking_id or "").strip()
                    or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                    "real_booking_id": b.booking_id,
                    "equipment_name": b.equipment.name if b.equipment else "",
                    "equipment_code": b.equipment.code if b.equipment else "",
                    "display_booking_id": (b.virtual_booking_id or "").strip()
                    or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                    "total_time_minutes": booking_effective_quota_minutes(b),
                    "user_name": (b.user.name or b.user.email) if b.user else "",
                }
            )

    events.sort(key=lambda e: (e["date"], e["real_booking_id"]))
    summary_message = (
        f"{total_minutes} minutes used out of {limit_minutes} minutes limit "
        f"({quota_type.lower()}, {quota_scope})"
    )
    return {
        "period_start": start_date.isoformat(),
        "period_end": end_date.isoformat(),
        "quota_type": quota_type,
        "quota_scope": quota_scope,
        "limit_minutes": limit_minutes,
        "total_minutes": total_minutes,
        "summary_message": summary_message,
        "events": events,
    }
