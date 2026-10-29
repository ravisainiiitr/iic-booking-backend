"""
Dynamic weekly external slot quota.

Replaces dedicated "Reserved for External" slots: external users may book any
AVAILABLE slot in their bookable window, up to a snapshotted weekly percentage
of bookable (AVAILABLE + BOOKED) slots.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from django.db import transaction
from django.db.models import Count, Q
from django.utils import timezone

from iic_booking.users.models.user_type import UserType

from .models import (
    BookingStatus,
    DailySlot,
    Equipment,
    ExternalWeeklySlotQuotaSnapshot,
    SlotStatus,
)

logger = logging.getLogger(__name__)

BOOKABLE_SLOT_STATUSES = (SlotStatus.AVAILABLE, SlotStatus.BOOKED)

# Bookings that still hold slots counted toward external usage.
EXTERNAL_USAGE_BOOKING_STATUSES = (
    BookingStatus.BOOKED,
    BookingStatus.COMPLETED,
)

EXTERNAL_QUOTA_SNAPSHOT_LEAD_MINUTES = 15


@dataclass(frozen=True)
class ExternalQuotaValidationResult:
    allowed: bool
    message: Optional[str] = None
    week_start: Optional[date] = None
    week_end: Optional[date] = None
    total_bookable_slots: Optional[int] = None
    external_quota_percent: Optional[int] = None
    max_external_slots: Optional[int] = None
    external_slots_consumed: Optional[int] = None
    slots_requested: Optional[int] = None
    remaining_external_slots: Optional[int] = None

    def as_error_payload(self) -> dict:
        return {
            "error": self.message or "External weekly slot quota exceeded.",
            "external_slot_quota": {
                "booking_week_start": self.week_start.isoformat() if self.week_start else None,
                "booking_week_end": self.week_end.isoformat() if self.week_end else None,
                "total_bookable_slots": self.total_bookable_slots,
                "external_quota_percent": self.external_quota_percent,
                "max_external_slots_allowed": self.max_external_slots,
                "external_slots_already_consumed": self.external_slots_consumed,
                "slots_requested": self.slots_requested,
                "remaining_external_slots": self.remaining_external_slots,
            },
        }


class ExternalSlotQuotaService:
    """Weekly snapshot generation, usage aggregation, and booking validation."""

    @staticmethod
    def week_bounds_for_date(d: date) -> tuple[date, date]:
        """Return (Monday, Sunday) for the ISO-style local week containing d."""
        week_start = d - timedelta(days=d.weekday())
        return week_start, week_start + timedelta(days=6)

    @classmethod
    def get_external_slot_window_date_bounds(cls, equipment, at=None) -> tuple[Optional[date], Optional[date], bool]:
        """
        Bookable date range for external users (mirrors internal window, shifted +1 week).

        Before this week's slot-window reference: next week only [W1].
        On/after reference: next week + next-to-next week [W1, W2].

        Returns (min_date, max_date, before_reference). All None/False when not configured.

        When the central external booking window (``external_services``) is enabled it replaces this rule for
        every equipment: its open range is returned (an empty range, min > max, while it is closed).
        """
        from iic_booking.external_services.window import legacy_bounds_or_policy

        central = legacy_bounds_or_policy(equipment, at)
        if central is not None:
            return central

        from iic_booking.equipment.api_views import (
            get_equipment_slot_window_reference_config,
            get_slot_window_reference_datetime_for_local_week,
        )

        rw, rt = get_equipment_slot_window_reference_config(equipment)
        if rw is None or rt is None:
            return None, None, False

        at = timezone.now() if at is None else at
        if timezone.is_naive(at):
            at = timezone.make_aware(at, timezone.get_current_timezone())
        local = timezone.localtime(at)
        week_monday = local.date() - timedelta(days=local.weekday())
        ref_dt = get_slot_window_reference_datetime_for_local_week(local, rw, rt)
        if ref_dt is None:
            return None, None, False

        w1_start = week_monday + timedelta(days=7)
        w1_end = w1_start + timedelta(days=6)
        w2_start = week_monday + timedelta(days=14)
        w2_end = w2_start + timedelta(days=6)

        if local < ref_dt:
            return w1_start, w1_end, True
        return w1_start, w2_end, False

    @classmethod
    def count_bookable_slots(cls, equipment, week_start: date, week_end: date) -> int:
        """Count slots that are bookable inventory: AVAILABLE or already BOOKED."""
        return DailySlot.objects.filter(
            slot_master__equipment=equipment,
            date__gte=week_start,
            date__lte=week_end,
            status__in=BOOKABLE_SLOT_STATUSES,
        ).count()

    @classmethod
    def compute_max_external_slots(cls, total_bookable: int, percent: int) -> int:
        percent = max(0, min(100, int(percent or 0)))
        return (int(total_bookable) * percent) // 100

    @classmethod
    def _ensure_week_slots(cls, equipment, week_start: date, week_end: date) -> None:
        """Generate the week's missing daily slots so they can be counted (never fails the caller)."""
        from .slot_utils import SlotGenerator

        try:
            SlotGenerator.generate_slots_for_week(equipment, week_start, week_end)
        except Exception:
            logger.exception(
                "external_slot_quota: slot generation failed equipment=%s week_start=%s",
                getattr(equipment, "equipment_id", None),
                week_start,
            )

    @classmethod
    def refresh_snapshot(cls, snap: ExternalWeeklySlotQuotaSnapshot, equipment) -> ExternalWeeklySlotQuotaSnapshot:
        """Bring a weekly snapshot up to date where that can only allow more bookings. ``snap`` must be locked.

        * A snapshot that counted no bookable slots was taken before the week's slots existed, so it never limited
          anything real: it is taken again (slot count and the current percentage) until it counts slots.
        * Otherwise the slot count stays as snapshotted; a share raised since then applies at once, a lowered one
          does not (bookings made within the old share stay valid)."""
        percent = max(0, min(100, int(getattr(equipment, "external_slot_quota_percent", 0) or 0)))
        if snap.total_bookable_slots:
            # A raised share applies at once (it only allows more bookings); a lowered one waits for the next
            # snapshot, so bookings already made within the old share stay valid.
            if percent > snap.external_quota_percent:
                snap.external_quota_percent = percent
                snap.max_external_slots = cls.compute_max_external_slots(snap.total_bookable_slots, percent)
                snap.save(update_fields=["external_quota_percent", "max_external_slots"])
                logger.info(
                    "external_slot_quota snapshot share raised equipment=%s week_start=%s percent=%s",
                    getattr(equipment, "equipment_id", None), snap.week_start, percent,
                )
            return snap
        total = cls.count_bookable_slots(equipment, snap.week_start, snap.week_end)
        if not total:
            return snap
        snap.total_bookable_slots = total
        snap.external_quota_percent = percent
        snap.max_external_slots = cls.compute_max_external_slots(total, percent)
        snap.save(update_fields=["total_bookable_slots", "external_quota_percent", "max_external_slots"])
        logger.info(
            "external_slot_quota snapshot retaken equipment=%s week_start=%s total=%s percent=%s",
            getattr(equipment, "equipment_id", None), snap.week_start, total, percent,
        )
        return snap

    @classmethod
    def ensure_snapshot(
        cls,
        equipment: Equipment,
        week_start: date,
        *,
        percent_override: Optional[int] = None,
    ) -> ExternalWeeklySlotQuotaSnapshot:
        """
        Create snapshot if missing; never overwrite an existing row.
        """
        week_start, week_end = cls.week_bounds_for_date(week_start)
        existing = ExternalWeeklySlotQuotaSnapshot.objects.filter(
            equipment=equipment,
            week_start=week_start,
        ).first()
        if existing:
            if existing.total_bookable_slots == 0:
                cls._ensure_week_slots(equipment, week_start, week_end)
                with transaction.atomic():
                    locked = ExternalWeeklySlotQuotaSnapshot.objects.select_for_update().get(pk=existing.pk)
                    return cls.refresh_snapshot(locked, equipment)
            return existing

        percent = (
            int(percent_override)
            if percent_override is not None
            else int(getattr(equipment, "external_slot_quota_percent", 0) or 0)
        )
        percent = max(0, min(100, percent))
        # The snapshot is taken two weeks ahead, usually before that week's slots exist: generate them first, or the
        # count (and so the cap) would be 0 for good.
        cls._ensure_week_slots(equipment, week_start, week_end)
        total = cls.count_bookable_slots(equipment, week_start, week_end)
        max_slots = cls.compute_max_external_slots(total, percent)

        snap, _created = ExternalWeeklySlotQuotaSnapshot.objects.get_or_create(
            equipment=equipment,
            week_start=week_start,
            defaults={
                "week_end": week_end,
                "total_bookable_slots": total,
                "external_quota_percent": percent,
                "max_external_slots": max_slots,
            },
        )
        return snap

    @classmethod
    def generate_due_snapshots(cls, now=None) -> int:
        """
        For each equipment (or global window) whose reference−15m ≤ now < reference,
        ensure a snapshot for W2 (week starting this_monday + 14).
        """
        from iic_booking.equipment.api_views import (
            get_equipment_slot_window_reference_config,
            get_slot_window_reference_datetime_for_local_week,
        )

        now = timezone.now() if now is None else now
        if timezone.is_naive(now):
            now = timezone.make_aware(now, timezone.get_current_timezone())
        local = timezone.localtime(now)
        created = 0

        equipments = Equipment.objects.all().only(
            "equipment_id",
            "slot_window_reference_weekday",
            "slot_window_reference_time",
            "external_slot_quota_percent",
        )
        for equipment in equipments.iterator():
            rw, rt = get_equipment_slot_window_reference_config(equipment)
            if rw is None or rt is None:
                continue
            ref_dt = get_slot_window_reference_datetime_for_local_week(local, rw, rt)
            if ref_dt is None:
                continue
            window_start = ref_dt - timedelta(minutes=EXTERNAL_QUOTA_SNAPSHOT_LEAD_MINUTES)
            if not (window_start <= local < ref_dt):
                continue
            week_monday = local.date() - timedelta(days=local.weekday())
            w2_start = week_monday + timedelta(days=14)
            before = ExternalWeeklySlotQuotaSnapshot.objects.filter(
                equipment=equipment, week_start=w2_start
            ).exists()
            cls.ensure_snapshot(equipment, w2_start)
            if not before:
                created += 1
                logger.info(
                    "external_slot_quota snapshot created equipment=%s week_start=%s",
                    equipment.equipment_id,
                    w2_start,
                )
        return created

    @classmethod
    def _external_booking_q(cls) -> Q:
        """Match bookings whose booker is an external user type."""
        codes = list(UserType.get_external_user_codes())
        q = Q()
        for code in codes:
            q |= Q(booking__user_type_snapshot__iexact=code)
            q |= Q(booking__user_type_snapshot="") & Q(booking__user__user_type=code)
            q |= Q(booking__user_type_snapshot__isnull=True) & Q(booking__user__user_type=code)
        return q

    @classmethod
    def count_external_usage(
        cls,
        equipment,
        week_start: date,
        week_end: date,
        *,
        exclude_booking_id: Optional[int] = None,
    ) -> int:
        """
        Count DailySlots in the week held by external bookings (BOOKED/COMPLETED),
        excluding repeat samples (source_booking set).

        Slots of an external special-request allocation awaiting payment (``external_services``) also count, so
        ordinary bookings cannot use quota an Officer in Charge has already allocated. Ordinary PENDING_PAYMENT
        bookings are not counted (unchanged).
        """
        from django.db import DatabaseError

        base = DailySlot.objects.filter(
            slot_master__equipment=equipment,
            date__gte=week_start,
            date__lte=week_end,
            status=SlotStatus.BOOKED,
            booking__isnull=False,
            booking__source_booking__isnull=True,
        ).filter(cls._external_booking_q()).exclude(booking__user__is_test_account=True)  # test accounts: exempt
        if exclude_booking_id is not None:
            base = base.exclude(booking_id=exclude_booking_id)
        with_special = Q(booking__status__in=EXTERNAL_USAGE_BOOKING_STATUSES) | Q(
            booking__status=BookingStatus.PENDING_PAYMENT,
            booking__external_special_allocation__state="awaiting_payment",
        )
        try:
            with transaction.atomic():  # savepoint: the external_services table may not be migrated yet
                return base.filter(with_special).count()
        except DatabaseError:
            logger.warning("special allocations unavailable; counting external usage without them", exc_info=True)
            return base.filter(booking__status__in=EXTERNAL_USAGE_BOOKING_STATUSES).count()

    @classmethod
    def validate_external_booking(
        cls,
        user,
        equipment: Equipment,
        *,
        slot_dates: list[date],
        slots_requested: int,
        exclude_booking_id: Optional[int] = None,
        bypass: bool = False,
    ) -> ExternalQuotaValidationResult:
        """
        Concurrency-safe check that an external booking does not exceed the weekly cap.
        """
        user_type = getattr(user, "user_type", None)
        if not UserType.is_external_user(user_type):
            return ExternalQuotaValidationResult(allowed=True)

        # Test accounts (external workflow pilot) are not limited by the share, and their bookings are not counted
        # against it (count_external_usage), so real external users keep the whole share.
        if bypass or getattr(user, "is_test_account", False) is True:
            return ExternalQuotaValidationResult(allowed=True)

        slots_requested = max(0, int(slots_requested or 0))
        if slots_requested <= 0:
            return ExternalQuotaValidationResult(allowed=True)

        if not slot_dates:
            return ExternalQuotaValidationResult(
                allowed=False,
                message="Cannot validate external slot quota without booking dates.",
                slots_requested=slots_requested,
            )

        # All slots in one booking should fall in one Mon–Sun week; use earliest date.
        primary = min(slot_dates)
        week_start, week_end = cls.week_bounds_for_date(primary)
        # Reject cross-week requests spanning different weeks.
        for d in slot_dates:
            ws, _ = cls.week_bounds_for_date(d)
            if ws != week_start:
                return ExternalQuotaValidationResult(
                    allowed=False,
                    message=(
                        "External bookings must fall within a single booking week "
                        f"({week_start.isoformat()} – {week_end.isoformat()})."
                    ),
                    week_start=week_start,
                    week_end=week_end,
                    slots_requested=slots_requested,
                )

        percent = int(getattr(equipment, "external_slot_quota_percent", 0) or 0)
        percent = max(0, min(100, percent))

        with transaction.atomic():
            # Lock any existing snapshot row for this week.
            snap = (
                ExternalWeeklySlotQuotaSnapshot.objects.select_for_update()
                .filter(equipment=equipment, week_start=week_start)
                .first()
            )
            if snap is None:
                # Lazy create once the week is (or should be) open / for admin testing.
                # Still immutable after create.
                total = cls.count_bookable_slots(equipment, week_start, week_end)
                max_slots = cls.compute_max_external_slots(total, percent)
                snap = ExternalWeeklySlotQuotaSnapshot.objects.create(
                    equipment=equipment,
                    week_start=week_start,
                    week_end=week_end,
                    total_bookable_slots=total,
                    external_quota_percent=percent,
                    max_external_slots=max_slots,
                )
                snap = (
                    ExternalWeeklySlotQuotaSnapshot.objects.select_for_update()
                    .get(pk=snap.pk)
                )
            else:
                snap = cls.refresh_snapshot(snap, equipment)

            consumed = cls.count_external_usage(
                equipment,
                week_start,
                week_end,
                exclude_booking_id=exclude_booking_id,
            )
            remaining = max(0, int(snap.max_external_slots) - consumed)

            if snap.max_external_slots <= 0 or consumed + slots_requested > snap.max_external_slots:
                msg = (
                    f"External weekly slot quota exceeded for booking week "
                    f"{week_start.isoformat()} – {week_end.isoformat()}. "
                    f"Total bookable slots: {snap.total_bookable_slots}; "
                    f"configured external quota: {snap.external_quota_percent}%; "
                    f"maximum external slots allowed: {snap.max_external_slots}; "
                    f"external slots already consumed: {consumed}; "
                    f"slots requested: {slots_requested}; "
                    f"remaining external slots: {remaining}."
                )
                return ExternalQuotaValidationResult(
                    allowed=False,
                    message=msg,
                    week_start=week_start,
                    week_end=week_end,
                    total_bookable_slots=snap.total_bookable_slots,
                    external_quota_percent=snap.external_quota_percent,
                    max_external_slots=snap.max_external_slots,
                    external_slots_consumed=consumed,
                    slots_requested=slots_requested,
                    remaining_external_slots=remaining,
                )

            return ExternalQuotaValidationResult(
                allowed=True,
                week_start=week_start,
                week_end=week_end,
                total_bookable_slots=snap.total_bookable_slots,
                external_quota_percent=snap.external_quota_percent,
                max_external_slots=snap.max_external_slots,
                external_slots_consumed=consumed,
                slots_requested=slots_requested,
                remaining_external_slots=remaining - slots_requested,
            )
