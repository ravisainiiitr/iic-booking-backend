# Check "download all" on production: booking BOOKING_PK (default 752) gets a direct storage link when it has a
# single stored file; the most recent booking whose results qualify for the storage-built ZIP gets it built by the
# Celery worker (state polled up to 3 minutes). Links are never printed. Run via `manage.py shell`.
import os
import time
from urllib.parse import urlparse

from django.db.models import Count, Q

from iic_booking.equipment.api_views import _booking_result_sources
from iic_booking.equipment.models import Booking
from iic_booking.equipment.results_download import (
    cached_zip_url,
    request_zip_build,
    zip_build_state,
    zip_cacheable,
    zip_fingerprint,
)

pk = int(os.environ.get("BOOKING_PK") or 752)
booking = Booking.objects.get(pk=pk)
sources = _booking_result_sources(booking)
print(f"booking={booking.virtual_booking_id} files={len(sources)} mb={sum(s.size for s in sources) / 1048576:.1f}")
if len(sources) == 1 and sources[0].direct_url:
    url = sources[0].direct_url(sources[0].filename) or ""
    print(f"single file direct link: {bool(url)} host={urlparse(url).hostname}")

candidates = (
    Booking.objects.annotate(n=Count("result_files", distinct=True))
    .filter(Q(n__gte=2))
    .order_by("-pk")[:40]
)
target = None
for b in candidates:
    s = _booking_result_sources(b)
    if zip_cacheable(s):
        target, target_sources = b, s
        break
if target is None:
    print("no booking currently qualifies for a storage-built ZIP")
else:
    fp = zip_fingerprint(target_sources)
    total = sum(s.size for s in target_sources)
    print(f"zip booking={target.virtual_booking_id} files={len(target_sources)} mb={total / 1048576:.1f}")
    print("already built:", bool(cached_zip_url(target.pk, fp, "check.zip")))
    print("request:", request_zip_build(target.pk, fp, total))
    deadline = time.monotonic() + 180
    last = None
    while time.monotonic() < deadline:
        state = zip_build_state(target.pk, fp) or {}
        if state != last:
            print(f"t={180 - (deadline - time.monotonic()):.0f}s state={state}")
            last = state
        if state.get("state") in ("ready", "failed"):
            break
        time.sleep(2)
    print("built link available:", bool(cached_zip_url(target.pk, fp, "check.zip")))
