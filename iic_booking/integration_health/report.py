"""Run the checks and fixes, keep the latest result for the dashboard, and email the Main Administrators when something
newly goes wrong (and once it is resolved). The nightly report also follows every booking of the last day through
each step (paid → I-STEM registered and confirmed → FBR executed after completion → WhatsApp delivered).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.utils import timezone

from .checks import FAIL, OFF, OK, WARN, Check, database_checks, worst

logger = logging.getLogger(__name__)

LATEST_KEY = "integration_health:latest"
STATE_KEY = "integration_health:alerted"  # {check key: status} last emailed
LATEST_TTL = 60 * 60 * 6
DASHBOARD_PATH = "/dashboard"


def run(*, heal: bool = True, live: bool = True, alert: bool = True, session=None) -> dict:
    """Checks (+ live checks), then safe fixes, then the checks again so the result shows what is still wrong."""
    from .heal import heal_all
    from .live import live_checks

    started = timezone.now()
    live_results, sender_state = (live_checks(session) if live else ([], None))
    fixes = heal_all(sender_state) if heal else {}
    checks = database_checks() + live_results
    payload = {
        "generated_at": timezone.now().isoformat(),
        "duration_ms": int((timezone.now() - started).total_seconds() * 1000),
        "overall": worst(c.status for c in checks if c.status != OFF),
        "checks": [c.as_dict() for c in checks],
        "fixes": fixes,
    }
    cache.set(LATEST_KEY, payload, LATEST_TTL)
    if alert:
        try:
            _alert_on_change(checks)
        except Exception:  # noqa: BLE001 - an alert that cannot be sent must not hide the result
            logger.exception("integration health: alert email failed")
    logger.info("integration health: overall=%s fixes=%s", payload["overall"], fixes)
    return payload


def latest() -> dict | None:
    return cache.get(LATEST_KEY)


# --- alerts ----------------------------------------------------------------------------------------------------------

def admin_recipients() -> list[str]:
    from iic_booking.users.models.user_type import UserType

    User = get_user_model()
    emails = (User.objects.filter(user_type=UserType.ADMIN, is_active=True, is_test_account=False)
              .exclude(email="").values_list("email", flat=True))
    return sorted({e for e in emails if e})


def _alert_on_change(checks: list[Check]) -> None:
    previous: dict = cache.get(STATE_KEY) or {}
    current = {c.key: c.status for c in checks if c.status != OFF}
    new_problems = [c for c in checks if c.status == FAIL and previous.get(c.key) != FAIL]
    resolved = [c for c in checks if previous.get(c.key) == FAIL and c.status in (OK, WARN)]
    cache.set(STATE_KEY, current, None)
    if not new_problems and not resolved:
        return
    subject = (f"Portal alert: {len(new_problems)} problem(s) found" if new_problems
               else f"Portal alert resolved: {', '.join(c.title for c in resolved)}")
    send_report(subject, new_problems, resolved=resolved)


def send_report(subject: str, problems: list[Check], *, resolved: list[Check] | None = None,
                journey: dict | None = None, recipients: list[str] | None = None) -> int:
    from iic_booking.communication.email_branding import wrap_email_html
    from iic_booking.communication.styled_transactional_emails import _send
    from iic_booking.communication.utils import get_frontend_absolute_url

    link = get_frontend_absolute_url(DASHBOARD_PATH)
    text, html = [], []
    for c in problems:
        text.append(f"[{c.status.upper()}] {c.title}: {c.summary}")
        text += [f"  - {i}" for i in c.items]
        if c.action:
            text.append(f"  What to do: {c.action}")
        html.append(f"<h3 style='margin:16px 0 4px'>{escape(c.title)}</h3><p style='margin:0'>{escape(c.summary)}</p>"
                    + ("<ul>" + "".join(f"<li>{escape(i)}</li>" for i in c.items) + "</ul>" if c.items else "")
                    + (f"<p style='margin:4px 0'><b>What to do:</b> {escape(c.action)}</p>" if c.action else ""))
    for c in resolved or []:
        text.append(f"[RESOLVED] {c.title}: {c.summary}")
        html.append(f"<p><b>Resolved:</b> {escape(c.title)} - {escape(c.summary)}</p>")
    if journey:
        text.append(journey_text(journey))
        html.append("<h3>Last 24 hours, booking by booking</h3><pre style='font-size:12px'>" + escape(journey_text(journey)) + "</pre>")
    text.append(f"Open the dashboard: {link}")
    body = "".join(html) + f"<p><a href='{escape(link)}'>Open the administration dashboard</a></p>"
    html_message = wrap_email_html(title=subject, body_inner_html=body, preheader=subject)
    sent = 0
    for email in recipients if recipients is not None else admin_recipients():
        _send(email, subject, "\n".join(text), html_message)
        sent += 1
    return sent


# --- nightly: follow every booking of the last day -------------------------------------------------------------------

def booking_journeys(since=None) -> dict:
    """Every booking made or completed since ``since`` (default 24 hours), and how far each got. Booking IDs only."""
    from iic_booking.equipment.models import Booking
    from iic_booking.whatsapp.models import WhatsAppMessage

    now = timezone.now()
    since = since or now - timedelta(hours=24)
    bookings = (Booking.objects.filter(created_at__gte=since) | Booking.objects.filter(completed_at__gte=since)) \
        .select_related("charge_profile").distinct().order_by("pk")[:500]
    rows, problems = [], []
    totals = {"bookings": 0, "istem_due": 0, "istem_registered": 0, "istem_executed_due": 0, "istem_executed": 0,
              "whatsapp_sent": 0, "whatsapp_refused": 0}
    for booking in bookings:
        totals["bookings"] += 1
        steps, missing = [booking.status], []
        istem = _istem_step(booking)
        if istem is not None:
            totals["istem_due"] += 1
            if istem["registered"]:
                totals["istem_registered"] += 1
                steps.append("I-STEM " + istem["state"])
            else:
                missing.append(f"not on I-STEM ({istem['state'] or 'not sent'})")
            if booking.status == "COMPLETED":
                totals["istem_executed_due"] += 1
                if istem["executed"]:
                    totals["istem_executed"] += 1
                elif booking.completed_at and booking.completed_at < now - timedelta(hours=2):
                    missing.append("FBR not executed")
        msgs = list(WhatsAppMessage.objects.filter(event_key__contains=f"booking:{booking.pk}:").values_list("status", "error"))
        ok = sum(1 for s, _e in msgs if s in ("sent", "delivered", "read"))
        bad = sum(1 for s, _e in msgs if s == "failed")
        totals["whatsapp_sent"] += ok
        totals["whatsapp_refused"] += bad
        if msgs:
            steps.append(f"WhatsApp {ok}/{len(msgs)}")
        if bad:
            missing.append(f"{bad} WhatsApp message(s) refused")
        ref = booking.virtual_booking_id or f"#{booking.pk}"
        rows.append({"booking": ref, "steps": steps, "missing": missing})
        if missing:
            problems.append(f"{ref}: " + "; ".join(missing))
    return {"since": since.isoformat(), "totals": totals, "rows": rows, "problems": problems}


def _istem_step(booking) -> dict | None:
    from iic_booking.equipment.models import charge_profile_requires_istem_fbr
    from iic_booking.istem import conf
    from iic_booking.istem.choices import SyncState

    if not conf.automatic_fbr_enabled() or not charge_profile_requires_istem_fbr(booking.charge_profile):
        return None
    try:
        link = booking.istem_link
    except Exception:  # noqa: BLE001 - not sent yet
        link = None
    state = getattr(link, "sync_state", "") or ""
    registered = bool(getattr(link, "request_id", None))
    executed = state in (SyncState.EXECUTED, SyncState.EXECUTED_PENDING_VERIFY)
    if booking.status not in ("BOOKED", "COMPLETED") and not registered:
        return None  # cancelled / unpaid bookings are not expected on I-STEM
    return {"state": state, "registered": registered, "executed": executed}


def journey_text(journey: dict) -> str:
    t = journey["totals"]
    lines = [
        f"Bookings: {t['bookings']}",
        f"I-STEM registered: {t['istem_registered']} of {t['istem_due']}",
        f"FBR executed: {t['istem_executed']} of {t['istem_executed_due']} completed",
        f"WhatsApp: {t['whatsapp_sent']} sent, {t['whatsapp_refused']} refused",
    ]
    if journey["problems"]:
        lines.append("Needs attention:")
        lines += [f"  - {p}" for p in journey["problems"][:50]]
    else:
        lines.append("Every booking went through each step.")
    return "\n".join(lines)


def nightly(*, email: bool = True) -> dict:
    payload = run(heal=True, live=True, alert=False)
    journey = booking_journeys()
    payload["journey"] = journey
    cache.set(LATEST_KEY, payload, LATEST_TTL)
    problems = [Check(**c) for c in payload["checks"] if c["status"] in (FAIL, WARN)]
    if email:
        state = "all clear" if payload["overall"] == OK and not journey["problems"] else \
            f"{len([p for p in problems if p.status == FAIL])} problem(s), {len(journey['problems'])} booking(s) to check"
        send_report(f"Portal nightly check: {state}", problems, journey=journey)
    return payload
