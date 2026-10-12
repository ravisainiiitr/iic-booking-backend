# Bookings awaiting a refund-vs-reschedule choice get DISRUPTION_DECISION_DAYS from the disruption mark.
# Moves each pending booking's decision deadline to mark + 5 days, only ever later. APPLY=1 writes; otherwise dry run.
# Run via `manage.py shell`.
import os
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.equipment.disruption_models import DisruptionEventSlot
from iic_booking.equipment.maintenance_policy import DISRUPTION_DECISION_DAYS
from iic_booking.equipment.models import Booking, BookingStatus

apply = os.environ.get("APPLY") == "1"
print(f"mode={'APPLY' if apply else 'DRY RUN'} window={DISRUPTION_DECISION_DAYS} days")


def fmt(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else "-"


def marked_at(booking):
    link = (
        DisruptionEventSlot.objects.filter(daily_slot__booking=booking)
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    if link:
        return link, "disruption record"
    return booking.updated_at, "booking last updated"


changed = 0
with transaction.atomic():
    for booking in Booking.objects.filter(status=BookingStatus.DISRUPTION_PENDING).order_by("booking_id"):
        mark, source = marked_at(booking)
        target = mark + timedelta(days=DISRUPTION_DECISION_DAYS)
        current = booking.maintenance_decision_deadline_at
        action = "extend" if current is None or target > current else "keep"
        print(
            f"  {booking.virtual_booking_id or booking.pk} | kind={booking.disruption_kind or '-'} "
            f"| marked={fmt(mark)} ({source}) | deadline {fmt(current)} -> {fmt(target) if action == 'extend' else 'unchanged'}"
        )
        if action == "extend":
            changed += 1
            if apply:
                booking.maintenance_decision_deadline_at = target
                booking.save(update_fields=["maintenance_decision_deadline_at"])

print(f"{'updated' if apply else 'would update'}: {changed}")
