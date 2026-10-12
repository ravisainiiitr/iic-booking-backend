# Read-only: bookings waiting on a user's refund-vs-reschedule choice after a lab disruption (operator absent etc.),
# and bookings sitting on Operator Absent slots that never entered that state. Run via `manage.py shell`.
from collections import Counter

from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus, SlotStatus

now = timezone.now()


def ref(b):
    return b.virtual_booking_id or str(b.pk)


def first_start(b):
    s = b.daily_slots.order_by("start_datetime").values_list("start_datetime", flat=True).first()
    return timezone.localtime(s).strftime("%Y-%m-%d %H:%M") if s else "-"


pending = list(
    Booking.objects.filter(status=BookingStatus.DISRUPTION_PENDING)
    .select_related("equipment")
    .order_by("booking_id")
)
print(f"DISRUPTION_PENDING bookings: {len(pending)}")
print("  by kind:", dict(Counter(b.disruption_kind or "(none)" for b in pending)))
print("  flag missing:", sum(1 for b in pending if not b.maintenance_disruption_flag))
print("  repeat bookings:", sum(1 for b in pending if b.source_booking_id))
print(
    "  decision deadline passed:",
    sum(1 for b in pending if b.maintenance_decision_deadline_at and b.maintenance_decision_deadline_at < now),
)
for b in pending:
    deadline = b.maintenance_decision_deadline_at
    print(
        f"  {ref(b)} | {b.equipment.code} | kind={b.disruption_kind or '-'} | flag={b.maintenance_disruption_flag} "
        f"| repeat={bool(b.source_booking_id)} | first_slot={first_start(b)} "
        f"| decide_by={timezone.localtime(deadline).strftime('%Y-%m-%d %H:%M') if deadline else '-'}"
    )

stuck = list(
    Booking.objects.filter(
        status__in=[BookingStatus.PENDING, BookingStatus.BOOKED],
        daily_slots__status=SlotStatus.OPERATOR_ABSENT,
    )
    .select_related("equipment")
    .distinct()
    .order_by("booking_id")
)
print(f"\nPENDING/BOOKED bookings with an Operator Absent slot: {len(stuck)}")
for b in stuck:
    print(f"  {ref(b)} | {b.equipment.code} | status={b.status} | first_slot={first_start(b)}")
