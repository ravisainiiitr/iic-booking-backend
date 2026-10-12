# Read-only: sample sets, stored minutes, booked slots, charge and edit history of one booking
# (piped into `manage.py shell` by the "Probe Booking Sample Sets" workflow; BOOKING_REF = display ID or pk).
import json
import os

from iic_booking.equipment.calculators import (
    TimeCalculationEngine,
    build_safe_input_values_for_charge_calculation,
    split_sample_sets,
)
from iic_booking.equipment.models import Booking

ref = (os.environ.get("BOOKING_REF") or "").strip()
qs = Booking.objects.select_related("equipment", "charge_profile")
booking = qs.filter(virtual_booking_id__iexact=ref).first() or (qs.filter(pk=int(ref)).first() if ref.isdigit() else None)
if booking is None:
    raise SystemExit(f"booking {ref!r} not found")

eq = booking.equipment
cp = booking.charge_profile
print(f"booking pk={booking.pk} ref={booking.virtual_booking_id} status={booking.status}")
print(f"equipment {eq.code} slot_duration={eq.slot_duration_minutes} profile_type={eq.profile_type}")
print(f"charge_profile id={cp.pk} type={cp.profile_type} user_type={cp.user_type} time_formula={cp.time_formula!r}")
print(f"stored total_time_minutes={booking.total_time_minutes} total_charge={booking.total_charge}")
print(f"pending_amount={booking.charge_recalculation_pending_amount}")
base, sets = split_sample_sets(booking.input_values or {})
print("sample set 1:", json.dumps(base, default=str))
for i, s in enumerate(sets, start=2):
    print(f"sample set {i}:", json.dumps(s, default=str))
safe = build_safe_input_values_for_charge_calculation(booking.input_values or {}, equipment=eq)
safe_base, safe_sets = split_sample_sets(safe)
for i, group in enumerate([safe_base, *safe_sets], start=1):
    mins = TimeCalculationEngine.calculate_time(cp, group, slot_duration_minutes=eq.slot_duration_minutes)
    print(f"engine minutes for sample set {i}: {mins}")
print("engine minutes total:", TimeCalculationEngine.calculate_time(cp, safe, slot_duration_minutes=eq.slot_duration_minutes))
print("charge_breakdown:", json.dumps(booking.charge_breakdown, default=str))
slots = list(booking.daily_slots.order_by("start_datetime").values("id", "start_datetime", "end_datetime", "status"))
print(f"slots ({len(slots)}):")
for s in slots:
    print(f"  {s['id']} {s['start_datetime']} -> {s['end_datetime']} {s['status']}")
print("events:")
for ev in booking.events.order_by("created_at"):
    meta = getattr(ev, "metadata", None)
    print(f"  {ev.created_at} {ev.event_type} {ev.previous_status}->{ev.new_status} | {(ev.comment or '')[:300]}")
    if meta:
        print(f"    metadata: {json.dumps(meta, default=str)[:600]}")
