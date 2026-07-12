"""Quota checking utilities for equipment bookings."""

from decimal import Decimal
from datetime import datetime, timedelta
from typing import Optional
from django.db.models import Sum, Count, Q, Exists, OuterRef
from django.utils import timezone
from .models import (
    Booking, BookingStatus, UserTypeQuota, ExternalUserQuota,
    QuotaType, QuotaLimitType, EquipmentGroupQuota,
)
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

QUOTA_COUNTING_STATUSES = (
    BookingStatus.BOOKED,
    BookingStatus.COMPLETED,
    BookingStatus.DISRUPTION_PENDING,
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


class QuotaChecker:
    """Utility class for checking quota limits."""

    @staticmethod
    def _sum_booking_quota_minutes(bookings_qs) -> int:
        bookings = list(bookings_qs.prefetch_related("daily_slots"))
        return sum(booking_effective_quota_minutes(b) for b in bookings)

    @staticmethod
    def _sum_booking_quota_charge(bookings_qs) -> Decimal:
        bookings = list(bookings_qs.prefetch_related("daily_slots"))
        total = Decimal("0.00")
        for b in bookings:
            total += booking_effective_quota_charge(b)
        return total.quantize(Decimal("0.01"))

    @staticmethod
    def check_user_quota(
        user: User,
        equipment,
        quota_type: str,
        additional_time_minutes: int = 0,
        additional_bookings: int = 0,
        additional_charge: Decimal = Decimal('0.00'),
        booking_date: Optional[datetime] = None,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """
        Check if user can make a booking based on quota limits.
        
        Uses group-level quotas if equipment belongs to a group, otherwise falls back
        to equipment-level quotas for backward compatibility.
        
        Args:
            user: User making the booking
            equipment: Equipment being booked
            quota_type: 'WEEKLY' or 'MONTHLY'
            additional_time_minutes: Additional time to be added (for new booking)
            additional_bookings: Additional bookings to be added (for new booking)
            additional_charge: Additional charge to be added (for new booking)
            booking_date: Date of the booking (for weekly/monthly quota period calculation).
                          If None, uses current date/time.
        
        Returns:
            Tuple of (is_allowed, error_message)
        """
        # Use booking_date if provided, otherwise use current time
        if booking_date is None:
            booking_date = timezone.now()
        
        # Check if equipment has a group - use group-level quotas
        # Refresh equipment from DB to ensure we have the latest equipment_group relationship
        equipment.refresh_from_db(fields=['equipment_group'])
        
        if equipment.equipment_group:
            return QuotaChecker._check_group_quota(
                user, equipment, quota_type, additional_time_minutes,
                additional_bookings, additional_charge, booking_date, exclude_booking_id
            )
        
        # Fallback to equipment-level quotas for backward compatibility
        user_type = user.user_type
        
        # Check if user is external
        if user.is_external():
            return QuotaChecker._check_external_quota(
                equipment, quota_type, additional_time_minutes,
                additional_bookings, additional_charge, booking_date, exclude_booking_id
            )
        else:
            return QuotaChecker._check_user_type_quota(
                equipment, user_type, quota_type, additional_time_minutes,
                additional_bookings, additional_charge, booking_date, exclude_booking_id
            )
    
    @staticmethod
    def _check_group_quota(
        user: User,
        equipment,
        quota_type: str,
        additional_time_minutes: int,
        additional_bookings: int,
        additional_charge: Decimal,
        booking_date: datetime,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """Check quota at equipment group level."""
        equipment_group = equipment.equipment_group
        
        # Get quota configuration for this group and quota type
        try:
            group_quota = EquipmentGroupQuota.objects.get(
                equipment_group=equipment_group,
                quota_type=quota_type,
                is_enforced=True
            )
            
        except EquipmentGroupQuota.DoesNotExist:
            # No quota configured for this group - allow booking
            return True, None
        
        # Get quota period dates based on booking date
        start_date, end_date = QuotaChecker._get_quota_period(quota_type, booking_date)
        
        # Determine if user is internal or external
        is_internal = UserType.is_internal_user(user.user_type)
        
        # Check if user should use faculty quota (faculty users OR users linked to faculty wallet)
        wallet = user.get_accessible_wallet()
        is_using_faculty_wallet = (
            wallet and 
            wallet.user.user_type == UserType.FACULTY and 
            wallet.user != user
        )
        is_faculty = user.is_faculty()
        use_faculty_quota = is_faculty or is_using_faculty_wallet
        
        # Get all equipment in this group
        group_equipment_ids = list(equipment_group.equipment.values_list('equipment_id', flat=True))
        
        # Check both individual and faculty quotas for every booking
        # Both quotas must pass for the booking to be allowed
        if is_internal:
            # Internal user - check internal quotas
            individual_quota_minutes = group_quota.internal_individual_quota_minutes
            faculty_quota_minutes = group_quota.internal_faculty_quota_minutes
            
            # Always check individual quota first
            individual_allowed, individual_error = QuotaChecker._check_individual_group_quota(
                user, group_equipment_ids, individual_quota_minutes,
                quota_type, start_date, end_date, additional_time_minutes, 'internal', booking_date, exclude_booking_id
            )
            
            if not individual_allowed:
                return False, individual_error
            
            # If user is faculty or using faculty wallet, also check faculty quota
            if use_faculty_quota:
                faculty_allowed, faculty_error = QuotaChecker._check_faculty_group_quota(
                    user, group_equipment_ids, faculty_quota_minutes,
                    quota_type, start_date, end_date, additional_time_minutes, 'internal', booking_date, exclude_booking_id
                )
                
                if not faculty_allowed:
                    return False, faculty_error
                return True, None
            else:
                return True, None
        else:
            # External user - check external quotas
            individual_quota_minutes = group_quota.external_individual_quota_minutes
            faculty_quota_minutes = group_quota.external_faculty_quota_minutes
            
            # Always check individual quota first
            individual_allowed, individual_error = QuotaChecker._check_individual_group_quota(
                user, group_equipment_ids, individual_quota_minutes,
                quota_type, start_date, end_date, additional_time_minutes, 'external', booking_date, exclude_booking_id
            )
            
            if not individual_allowed:
                return False, individual_error
            
            # If user is faculty or using faculty wallet, also check faculty quota
            if use_faculty_quota:
                faculty_allowed, faculty_error = QuotaChecker._check_faculty_group_quota(
                    user, group_equipment_ids, faculty_quota_minutes,
                    quota_type, start_date, end_date, additional_time_minutes, 'external', booking_date, exclude_booking_id
                )
                
                if not faculty_allowed:
                    return False, faculty_error
                
                return True, None
            else:
                return True, None
    
    @staticmethod
    def _check_individual_group_quota(
        user: User,
        group_equipment_ids,
        quota_limit_minutes: int,
        quota_type: str,
        start_date: datetime,
        end_date: datetime,
        additional_time_minutes: int,
        user_category: str,
        booking_date: Optional[datetime] = None,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """Check individual quota for a user across all equipment in the group."""
        
        if quota_limit_minutes <= 0:
            # No quota limit set - allow booking
            return True, None
        
        # Get existing bookings for this user across all equipment in the group
        # Filter by booking's scheduled date/time (from daily_slots), not created_at
        # Use a subquery to find bookings that have at least one slot in the period
        # This ensures we count each booking only once, even if it has multiple slots
        bookings_with_slots_in_period = Booking.objects.filter(
            pk=OuterRef('pk'),
            daily_slots__start_datetime__gte=start_date,
            daily_slots__start_datetime__lte=end_date
        )
        
        existing_bookings = Booking.objects.filter(
            user=user,
            equipment_id__in=group_equipment_ids,
            status__in=QUOTA_COUNTING_STATUSES,
        ).filter(
            Q(
                quota_period_anchor_at__isnull=False,
                quota_period_anchor_at__gte=start_date,
                quota_period_anchor_at__lte=end_date,
            )
            | (Q(quota_period_anchor_at__isnull=True) & Exists(bookings_with_slots_in_period))
        ).filter(
            source_booking__isnull=True  # Exclude repeat sample bookings from quota
        )
        if exclude_booking_id is not None:
            existing_bookings = existing_bookings.exclude(booking_id=exclude_booking_id)

        total_time = QuotaChecker._sum_booking_quota_minutes(existing_bookings)
        total_time += additional_time_minutes

        if total_time > quota_limit_minutes:
            error_msg = (
                f"{user_category.capitalize()} individual quota exceeded: {total_time} minutes used out of "
                f"{quota_limit_minutes} minutes limit ({quota_type.lower()})"
            )
            return False, error_msg

        return True, None

    @staticmethod
    def _check_faculty_group_quota(
        user: User,
        group_equipment_ids,
        quota_limit_minutes: int,
        quota_type: str,
        start_date: datetime,
        end_date: datetime,
        additional_time_minutes: int,
        user_category: str,
        booking_date: Optional[datetime] = None,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """Check faculty quota shared across all users linked to the same wallet."""
        if quota_limit_minutes <= 0:
            # No quota limit set - allow booking
            return True, None
        
        # Get the wallet this user can access
        wallet = user.get_accessible_wallet()
        if not wallet:
            # User doesn't have a wallet - treat as individual quota
            return QuotaChecker._check_individual_group_quota(
                user, group_equipment_ids, quota_limit_minutes,
                quota_type, start_date, end_date, additional_time_minutes, user_category, booking_date
            )
        
        # Get all users linked to this wallet
        # 1. The Supervisor
        wallet_users = [wallet.user]
        
        # 2. Students/Other users who have joined this wallet
        approved_requests = WalletJoinRequest.objects.filter(
            wallet=wallet,
            status=WalletJoinRequestStatus.APPROVED
        ).select_related('student')
        
        wallet_users.extend([req.student for req in approved_requests])
        
        # Get existing bookings for all users linked to this wallet across all equipment in the group
        # Filter by booking's scheduled date/time (from daily_slots), not created_at
        # Use a subquery to find bookings that have at least one slot in the period
        # This ensures we count each booking only once, even if it has multiple slots
        # IMPORTANT: This includes bookings on holidays, Saturdays, and Sundays within the week window
        bookings_with_slots_in_period = Booking.objects.filter(
            pk=OuterRef('pk'),
            daily_slots__start_datetime__gte=start_date,
            daily_slots__start_datetime__lte=end_date
        )
        
        existing_bookings = Booking.objects.filter(
            user__in=wallet_users,
            equipment_id__in=group_equipment_ids,
            status__in=QUOTA_COUNTING_STATUSES,
        ).filter(
            Q(
                quota_period_anchor_at__isnull=False,
                quota_period_anchor_at__gte=start_date,
                quota_period_anchor_at__lte=end_date,
            )
            | (Q(quota_period_anchor_at__isnull=True) & Exists(bookings_with_slots_in_period))
        ).filter(
            source_booking__isnull=True  # Exclude repeat sample bookings from quota
        )
        if exclude_booking_id is not None:
            existing_bookings = existing_bookings.exclude(booking_id=exclude_booking_id)

        total_time = QuotaChecker._sum_booking_quota_minutes(existing_bookings)
        total_time += additional_time_minutes

        if total_time > quota_limit_minutes:
            error_msg = (
                f"{user_category.capitalize()} faculty quota exceeded: {total_time} minutes used out of "
                f"{quota_limit_minutes} minutes limit ({quota_type.lower()}) "
                f"(shared across {len(wallet_users)} user(s) linked to the same wallet)"
            )
            return False, error_msg

        return True, None

    @staticmethod
    def _check_user_type_quota(
        equipment,
        user_type: str,
        quota_type: str,
        additional_time_minutes: int,
        additional_bookings: int,
        additional_charge: Decimal,
        booking_date: datetime,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """Check quota for a specific user type."""
        # Get quota period dates based on booking date
        start_date, end_date = QuotaChecker._get_quota_period(quota_type, booking_date)
        # Get all quotas for this equipment and user type
        quotas = UserTypeQuota.objects.filter(
            equipment=equipment,
            user_type=user_type,
            quota_type=quota_type,
            is_enforced=True
        )
        
        quota_list = list(quotas)
        if not quota_list:
            return True, None
        
        # Get existing bookings in the period
        # Filter by booking's scheduled date/time (from daily_slots), not created_at
        # Use a subquery to find bookings that have at least one slot in the period
        # This ensures we count each booking only once, even if it has multiple slots
        bookings_with_slots_in_period = Booking.objects.filter(
            pk=OuterRef('pk'),
            daily_slots__start_datetime__gte=start_date,
            daily_slots__start_datetime__lte=end_date
        )
        
        existing_bookings = Booking.objects.filter(
            equipment=equipment,
            user_type_snapshot=user_type,
            status__in=QUOTA_COUNTING_STATUSES,
        ).filter(
            Q(
                quota_period_anchor_at__isnull=False,
                quota_period_anchor_at__gte=start_date,
                quota_period_anchor_at__lte=end_date,
            )
            | (Q(quota_period_anchor_at__isnull=True) & Exists(bookings_with_slots_in_period))
        ).filter(
            source_booking__isnull=True  # Exclude repeat sample bookings from quota
        )
        if exclude_booking_id is not None:
            existing_bookings = existing_bookings.exclude(booking_id=exclude_booking_id)
        
        # Check each quota limit
        for quota in quota_list:
            if quota.limit_type == QuotaLimitType.HOURS:
                total_time = QuotaChecker._sum_booking_quota_minutes(existing_bookings)
                total_time += additional_time_minutes

                if total_time > quota.limit_value:
                    error_msg = (
                        f"Quota exceeded: {total_time} minutes used out of "
                        f"{quota.limit_value} minutes limit ({quota_type.lower()})"
                    )
                    return False, error_msg

            elif quota.limit_type == QuotaLimitType.BOOKINGS:
                total_bookings = existing_bookings.count() + additional_bookings

                if total_bookings > quota.limit_value:
                    error_msg = (
                        f"Quota exceeded: {total_bookings} bookings out of "
                        f"{quota.limit_value} bookings limit ({quota_type.lower()})"
                    )
                    return False, error_msg

            elif quota.limit_type == QuotaLimitType.CHARGE:
                total_charge = QuotaChecker._sum_booking_quota_charge(existing_bookings)
                total_charge += additional_charge

                if total_charge > quota.limit_value:
                    error_msg = (
                        f"Quota exceeded: ₹{total_charge} charged out of "
                        f"₹{quota.limit_value} limit ({quota_type.lower()})"
                    )
                    return False, error_msg

        return True, None

    @staticmethod
    def _check_external_quota(
        equipment,
        quota_type: str,
        additional_time_minutes: int,
        additional_bookings: int,
        additional_charge: Decimal,
        booking_date: datetime,
        exclude_booking_id: Optional[int] = None,
    ) -> tuple[bool, Optional[str]]:
        """Check quota for external users."""
        # Get quota period dates based on booking date
        start_date, end_date = QuotaChecker._get_quota_period(quota_type, booking_date)
        # Get all quotas for external users
        quotas = ExternalUserQuota.objects.filter(
            equipment=equipment,
            quota_type=quota_type,
            is_enforced=True
        )
        
        quota_list = list(quotas)
        if not quota_list:
            return True, None
        
        # Get existing bookings in the period for external users
        # Filter by booking's scheduled date/time (from daily_slots), not created_at
        # Use a subquery to find bookings that have at least one slot in the period
        # This ensures we count each booking only once, even if it has multiple slots
        bookings_with_slots_in_period = Booking.objects.filter(
            pk=OuterRef('pk'),
            daily_slots__start_datetime__gte=start_date,
            daily_slots__start_datetime__lte=end_date
        )
        
        existing_bookings = Booking.objects.filter(
            equipment=equipment,
            user_type_snapshot__in=['external', 'EXTERNAL'],
            status__in=QUOTA_COUNTING_STATUSES,
        ).filter(
            Q(
                quota_period_anchor_at__isnull=False,
                quota_period_anchor_at__gte=start_date,
                quota_period_anchor_at__lte=end_date,
            )
            | (Q(quota_period_anchor_at__isnull=True) & Exists(bookings_with_slots_in_period))
        ).filter(
            source_booking__isnull=True  # Exclude repeat sample bookings from quota
        )
        if exclude_booking_id is not None:
            existing_bookings = existing_bookings.exclude(booking_id=exclude_booking_id)
        
        # Check each quota limit
        for quota in quota_list:
            if quota.limit_type == QuotaLimitType.HOURS:
                total_time = QuotaChecker._sum_booking_quota_minutes(existing_bookings)
                total_time += additional_time_minutes

                if total_time > quota.limit_value:
                    error_msg = (
                        f"External user quota exceeded: {total_time} minutes used out of "
                        f"{quota.limit_value} minutes limit ({quota_type.lower()})"
                    )
                    return False, error_msg

            elif quota.limit_type == QuotaLimitType.BOOKINGS:
                total_bookings = existing_bookings.count() + additional_bookings

                if total_bookings > quota.limit_value:
                    error_msg = (
                        f"External user quota exceeded: {total_bookings} bookings out of "
                        f"{quota.limit_value} bookings limit ({quota_type.lower()})"
                    )
                    return False, error_msg

            elif quota.limit_type == QuotaLimitType.CHARGE:
                total_charge = QuotaChecker._sum_booking_quota_charge(existing_bookings)
                total_charge += additional_charge

                if total_charge > quota.limit_value:
                    error_msg = (
                        f"External user quota exceeded: ₹{total_charge} charged out of "
                        f"₹{quota.limit_value} limit ({quota_type.lower()})"
                    )
                    return False, error_msg

        return True, None
    
    @staticmethod
    def _get_quota_period(quota_type: str, reference_date: Optional[datetime] = None) -> tuple[datetime, datetime]:
        """
        Get start and end datetimes for a quota period based on reference date.

        Week/month boundaries are calendar periods in the active Django timezone
        (Asia/Kolkata). With USE_TZ=True, timezone.now() is UTC; weekday() and
        month must be taken from local time or bookings in the ~5.5h window
        around local midnight are attributed to the wrong period.

        Args:
            quota_type: 'WEEKLY' or 'MONTHLY'
            reference_date: Date to calculate quota period from (e.g., booking date).
                           If None, uses current date/time.

        Returns:
            Tuple of (start_date, end_date) as timezone-aware datetimes in the
            current local timezone (suitable for ORM comparisons).
        """
        if reference_date is None:
            reference_date = timezone.now()

        if timezone.is_naive(reference_date):
            reference_date = timezone.make_aware(
                reference_date, timezone.get_current_timezone()
            )
        # Compute calendar boundaries in local time, not UTC.
        reference_date = timezone.localtime(reference_date)

        if quota_type == QuotaType.WEEKLY:
            # Start of the week containing the reference date (Sunday to Saturday)
            # weekday() returns 0 for Monday, 1 for Tuesday, ..., 6 for Sunday
            # For Sunday-Saturday week:
            #   - If it's Sunday (weekday=6), days_since_sunday = 0
            #   - If it's Monday (weekday=0), days_since_sunday = 1
            #   - If it's Saturday (weekday=5), days_since_sunday = 6
            # Formula: (weekday + 1) % 7 gives us days since Sunday
            days_since_sunday = (reference_date.weekday() + 1) % 7
            start_date = reference_date - timedelta(days=days_since_sunday)
            start_date = start_date.replace(hour=0, minute=0, second=0, microsecond=0)
            # End date is end of Saturday (23:59:59.999999) - inclusive for the week
            # This ensures Sunday (next week) is NOT included
            saturday_date = start_date + timedelta(days=6)
            end_date = saturday_date.replace(hour=23, minute=59, second=59, microsecond=999999)

        elif quota_type == QuotaType.MONTHLY:
            # Start of the month containing the reference date
            start_date = reference_date.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            # End of the month containing the reference date (last day 23:59:59)
            if start_date.month == 12:
                next_month_start = start_date.replace(year=start_date.year + 1, month=1)
            else:
                next_month_start = start_date.replace(month=start_date.month + 1)
            # Last day of the month is one day before next month start
            last_day = next_month_start - timedelta(days=1)
            end_date = last_day.replace(hour=23, minute=59, second=59, microsecond=999999)

        else:
            raise ValueError(f"Invalid quota type: {quota_type}")

        return start_date, end_date


def get_quota_breakdown(user, equipment, quota_type: str, reference_date: datetime, failure_reason: str = ""):
    """
    Return date-wise breakdown of quota usage for display (admin/OIC).
    Used when a booking attempt failed due to weekly/monthly quota.
    
    Returns dict with: period_start, period_end, quota_type, quota_scope, limit_minutes,
    total_minutes, summary_message, events (list of {date, booking_id, equipment_name, total_time_minutes, user_name}).
    """
    from django.db.models import OuterRef, Exists
    
    start_date, end_date = QuotaChecker._get_quota_period(quota_type, reference_date)
    equipment.refresh_from_db(fields=['equipment_group'])
    events = []
    limit_minutes = 0
    total_minutes = 0
    quota_scope = "individual"
    
    bookings_with_slots_in_period = Booking.objects.filter(
        pk=OuterRef('pk'),
        daily_slots__start_datetime__gte=start_date,
        daily_slots__start_datetime__lte=end_date
    )
    
    if equipment.equipment_group:
        # Group quota path
        try:
            group_quota = EquipmentGroupQuota.objects.get(
                equipment_group=equipment.equipment_group,
                quota_type=quota_type,
                is_enforced=True
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
        
        group_equipment_ids = list(equipment.equipment_group.equipment.values_list('equipment_id', flat=True))
        is_internal = UserType.is_internal_user(user.user_type)
        use_faculty = "faculty" in (failure_reason or "").lower()
        
        if use_faculty:
            quota_scope = "faculty"
            wallet = user.get_accessible_wallet()
            if wallet:
                wallet_users = [wallet.user]
                wallet_users.extend([
                    req.student for req in
                    WalletJoinRequest.objects.filter(wallet=wallet, status=WalletJoinRequestStatus.APPROVED).select_related('student')
                ])
                limit_minutes = group_quota.internal_faculty_quota_minutes if is_internal else group_quota.external_faculty_quota_minutes
                existing_bookings = Booking.objects.filter(
                    user__in=wallet_users,
                    equipment_id__in=group_equipment_ids,
                    status__in=QUOTA_COUNTING_STATUSES,
                ).filter(Exists(bookings_with_slots_in_period)).filter(source_booking__isnull=True)
            else:
                existing_bookings = Booking.objects.none()
        else:
            limit_minutes = group_quota.internal_individual_quota_minutes if is_internal else group_quota.external_individual_quota_minutes
            existing_bookings = Booking.objects.filter(
                user=user,
                equipment_id__in=group_equipment_ids,
                status__in=QUOTA_COUNTING_STATUSES,
            ).filter(Exists(bookings_with_slots_in_period)).filter(source_booking__isnull=True)

        total_minutes = QuotaChecker._sum_booking_quota_minutes(existing_bookings)
        for b in existing_bookings.select_related('equipment', 'user').order_by('booking_id'):
            slot_date = b.daily_slots.filter(
                start_datetime__gte=start_date,
                start_datetime__lte=end_date
            ).order_by('date').values_list('date', flat=True).first()
            date_str = slot_date.strftime('%Y-%m-%d') if slot_date else ""
            events.append({
                "date": date_str,
                "booking_id": (b.virtual_booking_id or "").strip() or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                "real_booking_id": b.booking_id,
                "equipment_name": b.equipment.name if b.equipment else "",
                "equipment_code": b.equipment.code if b.equipment else "",
                "display_booking_id": (b.virtual_booking_id or "").strip() or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                "total_time_minutes": booking_effective_quota_minutes(b),
                "user_name": (b.user.name or b.user.email) if b.user else "",
            })
    else:
        # Equipment-level quota
        if user.is_external():
            quotas = ExternalUserQuota.objects.filter(
                equipment=equipment,
                quota_type=quota_type,
                is_enforced=True
            )
            existing_bookings = Booking.objects.filter(
                equipment=equipment,
                user_type_snapshot__in=['external', 'EXTERNAL'],
                status__in=QUOTA_COUNTING_STATUSES,
            ).filter(Exists(bookings_with_slots_in_period)).filter(source_booking__isnull=True)
            quota_scope = "external"
        else:
            user_type = user.user_type
            quotas = UserTypeQuota.objects.filter(
                equipment=equipment,
                user_type=user_type,
                quota_type=quota_type,
                is_enforced=True
            )
            existing_bookings = Booking.objects.filter(
                equipment=equipment,
                user_type_snapshot=user_type,
                status__in=QUOTA_COUNTING_STATUSES,
            ).filter(Exists(bookings_with_slots_in_period)).filter(source_booking__isnull=True)
            quota_scope = "user_type"

        quota_list = list(quotas)
        for q in quota_list:
            if getattr(q, 'limit_type', None) == QuotaLimitType.HOURS:
                limit_minutes = int(q.limit_value) if q.limit_value is not None else 0
                break
            elif hasattr(q, 'limit_value') and q.limit_value is not None:
                limit_minutes = int(q.limit_value)
                break

        total_minutes = QuotaChecker._sum_booking_quota_minutes(existing_bookings)
        for b in existing_bookings.select_related('equipment', 'user').order_by('booking_id'):
            slot_date = b.daily_slots.filter(
                start_datetime__gte=start_date,
                start_datetime__lte=end_date
            ).order_by('date').values_list('date', flat=True).first()
            date_str = slot_date.strftime('%Y-%m-%d') if slot_date else ""
            events.append({
                "date": date_str,
                "booking_id": (b.virtual_booking_id or "").strip() or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                "real_booking_id": b.booking_id,
                "equipment_name": b.equipment.name if b.equipment else "",
                "equipment_code": b.equipment.code if b.equipment else "",
                "display_booking_id": (b.virtual_booking_id or "").strip() or (f"{b.equipment.code}-{b.booking_id}" if b.equipment else str(b.booking_id)),
                "total_time_minutes": booking_effective_quota_minutes(b),
                "user_name": (b.user.name or b.user.email) if b.user else "",
            })
    
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

