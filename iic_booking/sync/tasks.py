"""Department Sync Celery tasks (beat schedule added by migration sync 0020)."""

from celery import shared_task


@shared_task(name="sync.evaluate_headless_agents")
def evaluate_headless_agents() -> dict:
    """Every 5 minutes: agent health, incidents (offline is only visible from the portal side), debounced
    Main Administrator alerts, and retention of heartbeat history / support bundles / old incidents."""
    from iic_booking.sync.services.headless import run_all

    return run_all()
