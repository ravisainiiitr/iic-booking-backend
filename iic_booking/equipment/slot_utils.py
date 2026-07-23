"""Slot generation and management utilities."""

from calendar import monthrange
from datetime import datetime, date, time, timedelta
from typing import List, Optional, Tuple
from django.utils import timezone
from django.core.exceptions import ValidationError
from .models import (
    Equipment, SlotMaster, DailySlot, SlotStatus, Holiday, EquipmentStatus
)


class SlotGenerator:
    """Utility class for generating daily slots."""

    @staticmethod
    def _aware_slot_datetimes(
        target_date: date,
        open_time: time,
        close_time: time,
    ) -> Tuple[datetime, datetime]:
        """Build timezone-aware start/end datetimes for a slot on target_date."""
        tz = timezone.get_current_timezone()
        start_datetime = timezone.make_aware(datetime.combine(target_date, open_time), tz)
        end_datetime = timezone.make_aware(datetime.combine(target_date, close_time), tz)
        if close_time < open_time:
            end_datetime += timedelta(days=1)
        return start_datetime, end_datetime

    @staticmethod
    def _end_date_after_months(start_date: date, months: int) -> date:
        """
        End date after advancing approximately `months` calendar months.

        Sums each traversed month's length via monthrange so months=1 and months>1
        use the same rule (no special-case 30-day approximation).
        """
        if months < 1:
            raise ValueError("months must be >= 1")
        current = start_date
        days_to_add = 0
        for _ in range(months):
            days_to_add += monthrange(current.year, current.month)[1]
            if current.month == 12:
                current = date(current.year + 1, 1, 1)
            else:
                current = date(current.year, current.month + 1, 1)
        return start_date + timedelta(days=days_to_add)

    @staticmethod
    def _get_initial_slot_status_for_equipment(equipment: Equipment) -> str:
        """
        Set initial DailySlot status based on the equipment's lifecycle state.
        """
        eq_status = (getattr(equipment, "status", None) or "").strip().upper()
        if eq_status in {EquipmentStatus.REPAIR, EquipmentStatus.INACTIVE}:
            return SlotStatus.UNDER_MAINTENANCE
        return SlotStatus.AVAILABLE
    
    @staticmethod
    def ensure_slot_masters_exist(equipment: Equipment, force_update: bool = False) -> List[SlotMaster]:
        """
        Return existing active SlotMasters for the equipment. Daily slots are generated from
        these using the same open_time/close_time for each date.

        If the equipment has no active slot masters, one default (9:00–18:00) is created
        so that the slots API returns data; admins can then edit or add slot masters in
        the Equipment change page (Slot Masters inline).

        Args:
            equipment: Equipment to get slot masters for
            force_update: Unused; kept for API compatibility.

        Returns:
            List of active SlotMaster instances for this equipment, ordered by slot_number.
        """
        existing_active = list(
            SlotMaster.objects.filter(equipment=equipment, is_active=True).order_by("slot_number")
        )
        if existing_active:
            return existing_active
        # No active slot masters: re-activate all existing ones if any (avoids duplicate key; keeps all slots)
        any_existing = list(
            SlotMaster.objects.filter(equipment=equipment).order_by("slot_number")
        )
        if any_existing:
            SlotMaster.objects.filter(equipment=equipment).update(is_active=True)
            return list(
                SlotMaster.objects.filter(equipment=equipment, is_active=True).order_by("slot_number")
            )
        default_open = time(0, 0)
        default_close = time(23, 59)
        default_master = SlotMaster.objects.create(
            equipment=equipment,
            slot_number=1,
            slot_name="Slot 1",
            open_time=default_open,
            close_time=default_close,
            is_active=True,
        )
        return [default_master]
    
    @staticmethod
    def generate_daily_slots(
        equipment: Equipment,
        target_date: date,
        slot_masters: Optional[List[SlotMaster]] = None,
        *,
        allow_holiday: bool = False
    ) -> List[DailySlot]:
        """
        Generate daily slots for a specific date based on Slot Master.
        Skips holidays (including Saturdays and Sundays) unless allow_holiday=True (e.g. for admin).
        
        Args:
            equipment: Equipment to generate slots for
            target_date: Date to generate slots for
            slot_masters: Optional list of slot masters to use (if None, uses all active)
            allow_holiday: If True, generate slots even on holidays/weekends (for admin booking).
        
        Returns:
            List of created DailySlot instances
        """
        if not allow_holiday:
            is_holiday, holiday_reason = Holiday.is_holiday(target_date)
            if is_holiday:
                return []
        
        # Get slot masters (if not provided)
        if slot_masters is None:
            slot_masters = list(SlotMaster.objects.filter(
                equipment=equipment,
                is_active=True
            ).order_by('slot_number'))

        initial_status = SlotGenerator._get_initial_slot_status_for_equipment(equipment)
        if allow_holiday and initial_status == SlotStatus.AVAILABLE:
            # Admin/OIC grid: weekend + table holidays are closed until staff sets e.g. AVAILABLE.
            is_holiday, _ = Holiday.is_holiday(target_date)
            if is_holiday:
                initial_status = SlotStatus.NOT_AVAILABLE
        if not slot_masters:
            return []

        # Single query: all existing slots for this equipment and date (key by slot_master_id + date to match DB unique constraint)
        existing_keys = set(
            DailySlot.objects.filter(
                slot_master__equipment=equipment,
                date=target_date
            ).values_list('slot_master_id', 'date')
        )

        to_create = []
        for slot_master in slot_masters:
            if (slot_master.pk, target_date) in existing_keys:
                continue
            start_datetime, end_datetime = SlotGenerator._aware_slot_datetimes(
                target_date, slot_master.open_time, slot_master.close_time
            )
            to_create.append(
                DailySlot(
                    slot_master=slot_master,
                    date=target_date,
                    start_datetime=start_datetime,
                    end_datetime=end_datetime,
                    status=initial_status,
                )
            )
        if to_create:
            return list(DailySlot.objects.bulk_create(to_create))
        return []

    @staticmethod
    def generate_slots_for_week(
        equipment: Equipment,
        week_start: date,
        week_end: date,
        *,
        allow_holiday: bool = False,
    ) -> List[DailySlot]:
        """
        Generate all missing daily slots for a week in one batch (single bulk_create).
        Faster than calling generate_daily_slots per day when loading the weekly slot window.
        Skips holidays unless allow_holiday=True.
        """
        holidays_in_range = Holiday.get_holidays_in_range(week_start, week_end)
        slot_masters = list(
            SlotMaster.objects.filter(equipment=equipment, is_active=True).order_by("slot_number")
        )
        if not slot_masters:
            return []
        initial_status = SlotGenerator._get_initial_slot_status_for_equipment(equipment)
        # Single query: existing (slot_master_id, date) in range
        existing_keys = set(
            DailySlot.objects.filter(
                slot_master__equipment=equipment,
                date__gte=week_start,
                date__lte=week_end,
            ).values_list("slot_master_id", "date")
        )
        to_create = []
        current_date = week_start
        while current_date <= week_end:
            if not allow_holiday and current_date in holidays_in_range:
                current_date += timedelta(days=1)
                continue
            for slot_master in slot_masters:
                if (slot_master.pk, current_date) in existing_keys:
                    continue
                start_datetime, end_datetime = SlotGenerator._aware_slot_datetimes(
                    current_date, slot_master.open_time, slot_master.close_time
                )
                row_status = initial_status
                if (
                    allow_holiday
                    and row_status == SlotStatus.AVAILABLE
                    and current_date in holidays_in_range
                ):
                    row_status = SlotStatus.NOT_AVAILABLE
                to_create.append(
                    DailySlot(
                        slot_master=slot_master,
                        date=current_date,
                        start_datetime=start_datetime,
                        end_datetime=end_datetime,
                        status=row_status,
                    )
                )
            current_date += timedelta(days=1)
        if to_create:
            return list(DailySlot.objects.bulk_create(to_create))
        return []

    @staticmethod
    def generate_weekly_slots(
        equipment: Equipment,
        start_date: date,
        end_date: Optional[date] = None
    ) -> List[DailySlot]:
        """
        Generate slots for a date range using batch week generation.
        Automatically skips holidays (including Sundays).
        
        Args:
            equipment: Equipment to generate slots for
            start_date: Start date
            end_date: End date (if None, generates for one week)
        
        Returns:
            List of created DailySlot instances
        """
        if end_date is None:
            end_date = start_date + timedelta(days=6)
        all_slots = []
        current = start_date
        while current <= end_date:
            week_end = min(current + timedelta(days=6), end_date)
            created = SlotGenerator.generate_slots_for_week(
                equipment, current, week_end, allow_holiday=False
            )
            all_slots.extend(created)
            current = week_end + timedelta(days=1)
        return all_slots
    
    @staticmethod
    def generate_monthly_slots(
        equipment: Equipment,
        start_date: Optional[date] = None,
        months: int = 1
    ) -> List[DailySlot]:
        """
        Generate slots forward for specified number of months based on Slot Master.
        Automatically skips holidays (including Sundays).
        
        Args:
            equipment: Equipment to generate slots for
            start_date: Start date (if None, uses today)
            months: Number of months forward to generate (default: 1)
        
        Returns:
            List of created DailySlot instances
        """
        if start_date is None:
            start_date = timezone.localdate()

        end_date = SlotGenerator._end_date_after_months(start_date, months)

        all_slots = []
        current = start_date
        while current <= end_date:
            week_end = min(current + timedelta(days=6), end_date)
            created = SlotGenerator.generate_slots_for_week(
                equipment, current, week_end, allow_holiday=False
            )
            all_slots.extend(created)
            current = week_end + timedelta(days=1)
        return all_slots


class SlotAvailabilityChecker:
    """Utility class for checking slot availability."""

    @staticmethod
    def _slot_has_started(daily_slot: DailySlot) -> bool:
        """True when the slot's start is missing or already in the past (local/UTC-aware)."""
        start = getattr(daily_slot, "start_datetime", None)
        if start is None:
            return True
        if timezone.is_naive(start):
            start = timezone.make_aware(start, timezone.get_current_timezone())
        return start <= timezone.now()

    @staticmethod
    def is_slot_available(daily_slot: DailySlot) -> bool:
        """
        Check if a daily slot is available for booking
        (internal users: any AVAILABLE; rejects past/started slots).
        """
        if SlotAvailabilityChecker._slot_has_started(daily_slot):
            return False
        return daily_slot.status == SlotStatus.AVAILABLE

    @staticmethod
    def is_slot_available_for_external(daily_slot: DailySlot) -> bool:
        """
        Check if a daily slot is available for external users
        (AVAILABLE + not past/started; same as internal — quota is enforced separately).
        """
        return SlotAvailabilityChecker.is_slot_available(daily_slot)
    
    @staticmethod
    def get_available_slots(
        equipment: Equipment,
        start_date: date,
        end_date: Optional[date] = None
    ) -> List[DailySlot]:
        """Get all available slots for a date range."""
        if end_date is None:
            end_date = start_date
        
        return DailySlot.objects.filter(
            slot_master__equipment=equipment,
            date__gte=start_date,
            date__lte=end_date,
            status=SlotStatus.AVAILABLE
        ).order_by('date', 'start_datetime')
    
    @staticmethod
    def get_available_slots_for_external(
        equipment: Equipment,
        start_date: date,
        end_date: Optional[date] = None
    ) -> List[DailySlot]:
        """Get AVAILABLE slots for external users (same pool as internal; quota enforced separately)."""
        return SlotAvailabilityChecker.get_available_slots(equipment, start_date, end_date)
    
    @staticmethod
    def block_slot(daily_slot: DailySlot, reason: str = "") -> None:
        """Block a slot (e.g., for maintenance)."""
        daily_slot.status = SlotStatus.BLOCKED
        daily_slot.save(update_fields=['status'])
    
    @staticmethod
    def unblock_slot(daily_slot: DailySlot) -> None:
        """Unblock a slot."""
        # Only unblock if not booked
        if daily_slot.status == SlotStatus.BLOCKED:
            daily_slot.status = SlotStatus.AVAILABLE
            daily_slot.save(update_fields=['status'])
