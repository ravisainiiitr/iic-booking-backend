# Read-only timing of the results download path on the server, hop by hop, for one large result file
# (BOOKING_REF, else the largest operator result file uploaded in the last 2 days). Each test reads at most
# PROBE_MB megabytes and discards them. Writes the ZIP link and an S3 link for the host-side curl tests to
# /tmp/results_probe_urls (never printed). Run via `manage.py shell`.
import json
import os
import time
from datetime import timedelta

from django.db.models import Q
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingResultFile
from iic_booking.equipment.results_download import (
    CHUNK_SIZE,
    booking_zip_members,
    iter_zip_stream,
    make_download_token,
    open_storage_file_stream,
    storage_file_direct_url,
)
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType

LIMIT = int(os.environ.get("PROBE_MB") or 100) * 1024 * 1024
ref = (os.environ.get("BOOKING_REF") or "").strip()


def report(name, nbytes, seconds, **extra):
    mbps = nbytes / 1024 / 1024 / seconds if seconds else 0
    print(json.dumps({"test": name, "mb": round(nbytes / 1024 / 1024, 1), "s": round(seconds, 2),
                      "MB_per_s": round(mbps, 1), **extra}))


def drain(chunks):
    n, start = 0, time.monotonic()
    for chunk in chunks:
        n += len(chunk)
        if n >= LIMIT:
            break
    return n, time.monotonic() - start


if ref:
    booking = Booking.objects.get(Q(virtual_booking_id=ref) | Q(pk=int(ref) if ref.isdigit() else -1))
    brf = BookingResultFile.objects.filter(booking=booking).order_by("-created_at").first()
else:
    recent = BookingResultFile.objects.filter(created_at__gte=timezone.now() - timezone.timedelta(days=2))
    brf = max(recent, key=lambda r: r.file.size if r.file else 0)
    booking = brf.booking
print(json.dumps({"booking": booking.virtual_booking_id, "file": brf.original_name, "file_mb": round(brf.file.size / 1048576, 1)}))

# 1. Storage -> server (S3 GetObject body read in CHUNK_SIZE pieces)
body = open_storage_file_stream(brf.file)
n, s = drain(iter(lambda: body.read(CHUNK_SIZE), b""))
body.close()
report("s3_to_server", n, s)

# 2. ZIP built in the app (S3 read + CRC + zip framing), no HTTP
members, total = booking_zip_members(booking)
n, s = drain(iter_zip_stream(members))
report("zip_build_in_app", n, s, total_mb=round(total / 1048576, 1), members=len(members))

# 3. Links for the host: ZIP through the app, and a direct S3 link (staff user: downloads are not tracked)
admin = User.objects.filter(user_type=UserType.ADMIN, is_active=True).order_by("pk").first()
path = f"/api/bookings/{booking.pk}/results/download/?t={make_download_token(admin, booking.pk)}"
with open("/tmp/results_probe_urls", "w") as fh:
    fh.write(path + "\n")
    fh.write((storage_file_direct_url(brf.file, brf.original_name or "result.bin") or "") + "\n")
print("urls written")
