"""Run one external-services scheduled task once (no Celery worker or beat locally). Usage: run_task.py <name>"""

import sys

from _bootstrap import setup

setup()

from django.utils import timezone  # noqa: E402

from iic_booking.external_services import tasks  # noqa: E402

ALLOWED = (
    "prepare_window", "expire_special_allocations", "queue_completed_invoice_requests", "sric_invoice_batch",
    "notifications_sweep",
)

name = sys.argv[1] if len(sys.argv) > 1 else ""
if name not in ALLOWED:
    sys.exit(f"Unknown task {name!r}; one of: {', '.join(ALLOWED)}")
print(f"now (IST): {timezone.localtime():%Y-%m-%d %H:%M:%S}")
print(f"{name}: {getattr(tasks, name)()}")
