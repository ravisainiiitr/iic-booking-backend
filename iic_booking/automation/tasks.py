from __future__ import annotations

import logging

from celery import shared_task

from iic_booking.equipment.peak_window import defer_during_peak

logger = logging.getLogger(__name__)


@shared_task(name="automation.send_weekly_oic_anomaly_alerts")
@defer_during_peak("automation.send_weekly_oic_anomaly_alerts")
def send_weekly_oic_anomaly_alerts() -> dict:
    """Monday 09:15 IST. Does nothing unless the admin switch is on."""
    from .audit import settings_row
    from .oic_alerts import run

    if not settings_row().oic_alerts_enabled:
        return {"skipped": "switched off"}
    out = run(dry_run=False)
    logger.info("weekly oic anomaly alerts: week=%s flagged=%s sent=%s", out["week"], out["equipment_flagged"], out["sent"])
    return {k: out[k] for k in ("week", "equipment_flagged", "recipients", "sent", "skipped")}


@shared_task(name="automation.send_no_show_prompts")
@defer_during_peak("automation.send_no_show_prompts")
def send_no_show_prompts() -> dict:
    """Daily 17:30 IST. Does nothing unless the admin switch is on."""
    from .audit import settings_row
    from .no_show import run

    if not settings_row().no_show_prompts_enabled:
        return {"skipped": "switched off"}
    out = run(dry_run=False)
    logger.info("no-show prompts: bookings=%s emails=%s", out["bookings"], out["emails_sent"])
    return {k: out[k] for k in ("bookings", "recipients", "emails_sent")}
