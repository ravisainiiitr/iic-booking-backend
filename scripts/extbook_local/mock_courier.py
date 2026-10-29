"""Local-only TrackCourier.io stand-in for acceptance testing (never part of the deployed application).

Only the provider boundary is replaced; the application's courier-tracking code runs unchanged:

* ``catalog``: loads a small synthetic courier list through the real ``services.refresh_catalog`` with a fake client.
* ``event <booking_id> <status>``: the provider reports a status for that booking's active courier shipment. The event
  is signed with a synthetic secret (set for this process only) and goes through the real ``webhooks.accept`` (signature
  check, durable storage, de-duplication) and ``webhooks.process`` (parsing, forward-only apply, milestone notices to
  the user, delivery notice to the lab staff).

Statuses: info_received, in_transit, out_for_delivery, delivered, attempt_fail, exception, returned.
Refuses to load unless the acceptance settings are active. No network access is involved.

CLI: ``extbook-local.ps1 courier catalog`` / ``extbook-local.ps1 courier <booking_id> -Outcome <status>``
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from datetime import timedelta

SYNTHETIC_WEBHOOK_SECRET = "acceptance-local-courier-secret-not-real"
CATALOG = [
    {"slug": "bluedart", "name": "Blue Dart"},
    {"slug": "delhivery", "name": "Delhivery"},
    {"slug": "dtdc", "name": "DTDC"},
    {"slug": "india-post", "name": "India Post"},
]
LABELS = {
    "info_received": "Shipment information received",
    "in_transit": "In transit — departed origin hub",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "attempt_fail": "Delivery attempt failed — addressee not available",
    "exception": "Shipment held — address issue",
    "returned": "Returned to sender",
}


def assert_acceptance() -> None:
    from django.conf import settings

    if not getattr(settings, "ACCEPTANCE_LOCAL", False) or settings.SETTINGS_MODULE != "config.settings.acceptance_local":
        raise RuntimeError("mock_courier runs only under config.settings.acceptance_local")


class _FakeClient:
    def list_couriers(self):
        from iic_booking.courier_tracking.client import ApiResult
        from iic_booking.courier_tracking.constants import LookupOutcome

        return ApiResult(True, LookupOutcome.SUCCESS, data=[dict(c) for c in CATALOG])


def load_catalog() -> int:
    from iic_booking.courier_tracking import services

    assert_acceptance()
    ok = services.refresh_catalog(client=_FakeClient())
    print(f"synthetic courier catalog loaded: {ok} ({', '.join(c['name'] for c in CATALOG)})")
    return 0 if ok else 2


def deliver(booking_id: int, status: str) -> int:
    from django.test import override_settings
    from django.utils import timezone

    from iic_booking.courier_tracking import webhooks
    from iic_booking.courier_tracking.constants import ArrivalMode
    from iic_booking.courier_tracking.models import SampleArrival

    assert_acceptance()
    if status not in LABELS:
        print(f"unknown status {status!r}; use one of: {', '.join(LABELS)}")
        return 3
    arrival = SampleArrival.objects.filter(booking_id=booking_id, is_active=True,
                                           arrival_mode=ArrivalMode.COURIER).first()
    if arrival is None:
        print(f"booking {booking_id} has no active courier shipment; register one on the booking page first")
        return 3
    now = timezone.localtime()
    checkpoint = {"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"), "status": status,
                  "message": LABELS[status], "location": "Roorkee" if status in ("out_for_delivery", "delivered")
                  else "Delhi hub"}
    payload = {
        "event": "tracking.delivered" if status == "delivered" else "tracking.updated",
        "enqueued_at": (timezone.now() + timedelta(seconds=1)).isoformat(),
        "data": {
            "courier": arrival.courier_slug,
            "tracking_number": arrival.tracking_number,
            "status": status,
            "most_recent_status": LABELS[status],
            "checkpoints": [checkpoint],
            "delivered_date": now.strftime("%Y-%m-%d") if status == "delivered" else "",
            "nonce": f"{time.time():.6f}",  # each simulated event is a distinct delivery
        },
    }
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(SYNTHETIC_WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    with override_settings(TRACKCOURIER_WEBHOOK_SECRET=SYNTHETIC_WEBHOOK_SECRET):
        event = webhooks.accept(body, signature, mode_header="test")
        outcome = webhooks.process(event.pk)
    arrival.refresh_from_db()
    print(f"simulated '{status}' for booking {booking_id}: event {outcome}; shipment now {arrival.status}")
    return 0


if __name__ == "__main__":
    from _bootstrap import setup

    setup()
    if len(sys.argv) < 2 or sys.argv[1] == "catalog":
        sys.exit(load_catalog())
    sys.exit(deliver(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "in_transit"))
