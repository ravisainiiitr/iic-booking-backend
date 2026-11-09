"""Live checks: one read-only call to each outside service the booking flow depends on.

Nothing is sent to users and nothing is changed: an I-STEM token, Meta's report on the WhatsApp sender, one Razorpay
list call, and an SMTP connection that is opened and closed without a message. Results carry no token, key, phone
number or email address.
"""

from __future__ import annotations

import smtplib
import ssl

import requests
from django.conf import settings

from .checks import FAIL, OFF, OK, WARN, Check

TIMEOUT = 20


def istem_live() -> Check:
    from iic_booking.istem import conf, service

    check = Check("istem_live", "istem", "I-STEM answers")
    if not conf.automatic_fbr_enabled():
        check.status, check.summary = OFF, "Automatic I-STEM records are switched off."
        return check
    missing = conf.missing_live_settings()
    if missing:
        check.status, check.summary = FAIL, f"I-STEM settings missing: {', '.join(missing)}."
        return check
    try:
        info = service.default_client().session_info()
    except Exception as exc:  # noqa: BLE001
        check.status = FAIL
        check.summary = f"I-STEM did not issue a token ({type(exc).__name__}: {str(getattr(exc, 'message', '') or '')[:120]})."
        check.action = "Check the I-STEM credentials and whether the I-STEM server is reachable."
        return check
    check.summary = f"I-STEM issued a token (institute {info.institute_id or '-'})."
    return check


def whatsapp_sender_state(session=None) -> dict:
    """Meta's report on the configured sender: {'can_send': 'AVAILABLE'|'LIMITED'|'BLOCKED'|'', 'notes': [...]}."""
    from iic_booking.whatsapp import conf

    url = f"{conf.GRAPH_BASE}/{conf.api_version()}/{conf.phone_number_id()}"
    resp = (session or requests).get(url, timeout=TIMEOUT, params={"fields": "verified_name,name_status,status,quality_rating,health_status"},
                                     headers={"Authorization": f"Bearer {conf.access_token()}"})
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        err = (data.get("error") or {}) if isinstance(data, dict) else {}
        raise RuntimeError(f"Meta answered HTTP {resp.status_code} ({err.get('code', '')})")
    health = data.get("health_status") or {}
    notes = []
    for entity in health.get("entities") or []:
        notes += [str(n)[:200] for n in entity.get("additional_info") or []]
        notes += [str(e.get("error_description") or e.get("possible_solution") or "")[:200] for e in entity.get("errors") or []]
    return {"can_send": str(health.get("can_send_message") or ""), "notes": [n for n in notes if n],
            "name": str(data.get("verified_name") or ""), "quality": str(data.get("quality_rating") or ""),
            "status": str(data.get("status") or "")}


def whatsapp_live(session=None) -> tuple[Check, dict | None]:
    from iic_booking.whatsapp import conf

    check = Check("whatsapp_live", "whatsapp", "Meta accepts messages from the WhatsApp number")
    if not conf.enabled():
        check.status, check.summary = OFF, "WhatsApp messages are switched off."
        return check, None
    try:
        state = whatsapp_sender_state(session)
    except Exception as exc:  # noqa: BLE001
        check.status, check.summary = WARN, f"Meta's report could not be read ({type(exc).__name__}: {str(exc)[:120]})."
        return check, None
    can = state["can_send"]
    if can == "AVAILABLE":
        check.summary = f"Meta accepts messages from \"{state['name']}\" (quality {state['quality'] or '-'})."
    else:
        check.status = FAIL if can in ("BLOCKED", "LIMITED") else WARN
        check.summary = f"Meta reports sending as {can or 'unknown'} for \"{state['name']}\"."
        check.items = state["notes"][:5]
        check.action = ("Open Meta WhatsApp Manager → Phone numbers and settle what Meta lists (usually the display name "
                        "approval and business verification).")
    return check, state


def payments_live() -> Check:
    from iic_booking.payments import gateway_mode
    from iic_booking.payments.razorpay_service import get_razorpay_client

    check = Check("payments_live", "payments", "Razorpay answers")
    if not gateway_mode.enabled():
        check.status, check.summary = OFF, "Online payments are switched off."
        return check
    try:
        get_razorpay_client().order.all({"count": 1})
    except Exception as exc:  # noqa: BLE001
        check.status = FAIL
        check.summary = f"Razorpay refused the read-only call ({type(exc).__name__})."
        check.action = "Check the Razorpay keys and mode in the server settings."
        return check
    check.summary = f"Razorpay answered ({gateway_mode.mode()} mode)."
    return check


def email_live() -> Check:
    check = Check("email_live", "email", "Mail server accepts connections")
    backend = str(getattr(settings, "EMAIL_BACKEND", ""))
    if "smtp" not in backend.lower():
        check.summary = f"Email is sent through {backend.rsplit('.', 1)[-1] or 'the configured backend'} (not checked here)."
        return check
    host, port = getattr(settings, "EMAIL_HOST", ""), int(getattr(settings, "EMAIL_PORT", 25) or 25)
    try:
        if getattr(settings, "EMAIL_USE_SSL", False):
            conn = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            conn = smtplib.SMTP(host, port, timeout=TIMEOUT)
            if getattr(settings, "EMAIL_USE_TLS", False):
                conn.starttls(context=ssl.create_default_context())
        user, password = getattr(settings, "EMAIL_HOST_USER", ""), getattr(settings, "EMAIL_HOST_PASSWORD", "")
        if user and password:
            conn.login(user, password)
        conn.quit()
    except Exception as exc:  # noqa: BLE001
        check.status = FAIL
        check.summary = f"The mail server refused the connection ({type(exc).__name__})."
        check.action = "Check the mail server settings; no booking email can go out until this is fixed."
        return check
    check.summary = "The mail server accepted a connection and sign-in (no message sent)."
    return check


def live_checks(session=None) -> tuple[list[Check], dict | None]:
    """All live checks; also returns Meta's sender state for the WhatsApp resend decision."""
    out = []
    for fn in (istem_live, payments_live, email_live):
        try:
            out.append(fn())
        except Exception as exc:  # noqa: BLE001
            out.append(Check(fn.__name__, fn.__name__.split("_")[0], fn.__name__, status=WARN,
                             summary=f"The check could not run ({type(exc).__name__})."))
    wa, state = whatsapp_live(session)
    out.append(wa)
    return out, state
