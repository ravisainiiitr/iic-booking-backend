# Read-only: status, result files and recent events of one booking (VIRTUAL_ID), plus the newest result-file
# uploads portal-wide. Run via `manage.py shell`.
import os

from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingResultFile

vid = (os.environ.get("VIRTUAL_ID") or "").strip()
booking = Booking.objects.filter(virtual_booking_id=vid).select_related("equipment").first()
if booking is None:
    print(f"no booking {vid!r}")
else:
    print(
        f"booking pk={booking.pk} vid={booking.virtual_booking_id} eq={booking.equipment.code} status={booking.status}"
        f" completed_at={booking.completed_at}"
    )
    for rf in BookingResultFile.objects.filter(booking=booking).order_by("pk"):
        try:
            size = rf.file.size
        except Exception as exc:
            size = f"ERR {type(exc).__name__}"
        print(f"  file pk={rf.pk} name={rf.original_name!r} size={size} created={getattr(rf, 'uploaded_at', None) or getattr(rf, 'created_at', None)}")
    for ev in booking.events.order_by("-pk")[:8]:
        print(f"  event {ev.created_at:%Y-%m-%d %H:%M:%S} {ev.event_type} {ev.previous_status}->{ev.new_status} {str(ev.comment)[:120]!r}")

since = timezone.now() - timezone.timedelta(days=3)
date_field = "uploaded_at" if hasattr(BookingResultFile, "uploaded_at") else "created_at"
recent = BookingResultFile.objects.filter(**{f"{date_field}__gte": since}).select_related("booking").order_by("-pk")[:25]
print(f"result files uploaded in the last 3 days (newest 25, by {date_field}):")
for rf in recent:
    print(f"  {getattr(rf, date_field):%m-%d %H:%M} booking={rf.booking.virtual_booking_id} name={rf.original_name!r}")
