import logging

from celery import shared_task

from . import access

logger = logging.getLogger(__name__)


@shared_task(name="training.housekeeping")
def housekeeping() -> dict:
    """Expire unanswered demo proposals and seat offers, promote waitlisted students, escalate overdue reviews."""
    if not access.module_enabled():
        return {"skipped": "disabled"}
    from . import demo, selection

    out = {}
    for name, fn in (
        ("proposals_expired", demo.expire_proposals),
        ("seats_expired", selection.expire_unconfirmed),
        ("demo_escalated", demo.escalate_overdue),
    ):
        try:
            out[name] = fn()
        except Exception:
            logger.exception("training housekeeping step %s failed", name)
            out[name] = "error"
    return out
