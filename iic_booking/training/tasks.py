import logging

from celery import shared_task

from iic_booking.equipment.peak_window import defer_during_peak

from . import access

logger = logging.getLogger(__name__)


@shared_task(name="training.housekeeping")
@defer_during_peak("training.housekeeping")
def housekeeping() -> dict:
    """Expire unanswered demo proposals and seat offers, promote waitlisted students, escalate overdue reviews,
    expire/remind certifications, remind and release unconfirmed duty, and close finished duty shifts."""
    if not access.module_enabled():
        return {"skipped": "disabled"}
    from . import certification, demo, duty, selection

    out = {}
    for name, fn in (
        ("proposals_expired", demo.expire_proposals),
        ("seats_expired", selection.expire_unconfirmed),
        ("demo_escalated", demo.escalate_overdue),
        ("certifications", certification.housekeeping),
        ("duty", duty.housekeeping),
    ):
        try:
            out[name] = fn()
        except Exception:
            logger.exception("training housekeeping step %s failed", name)
            out[name] = "error"
    return out
