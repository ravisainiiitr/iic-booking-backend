"""Copy of every new support ticket to the recipient list configured by the Main Administrator."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from django.conf import settings
from django.core.mail import send_mail
from django.db import close_old_connections, transaction
from django.utils.html import escape

from .models import SupportNotificationSettings, Ticket

logger = logging.getLogger(__name__)

PUBLIC_REQUESTER_LABEL = "Public (not signed in)"


def configured_alert_emails() -> list[str]:
    """Addresses configured by the Main Administrator; empty when alerts are switched off."""
    from iic_booking.users.test_accounts import parse_email_list

    cfg = SupportNotificationSettings.get_singleton()
    if not cfg.ticket_alert_enabled:
        return []
    return parse_email_list(cfg.ticket_alert_emails)


def _dedupe(emails, *, exclude=()) -> list[str]:
    seen = {(e or "").strip().lower() for e in exclude if e}
    out: list[str] = []
    for e in emails:
        value = (e or "").strip()
        if not value or "@" not in value or value.lower() in seen:
            continue
        seen.add(value.lower())
        out.append(value)
    return out


def alert_recipients_for(ticket: Ticket) -> list[str]:
    """
    Delivery list for the new-ticket copy.

    Tickets raised by test accounts go only to the test-account inbox; configured addresses that
    belong to test accounts are redirected the same way as other portal mail. The assignee already
    gets the assignment email, so they are not sent a second copy.
    """
    from iic_booking.users.test_accounts import email_redirects, is_test_user, redirect_email_address

    configured = configured_alert_emails()
    if not configured:
        return []

    if is_test_user(getattr(ticket, "user", None)):
        delivery = list(email_redirects())
    else:
        delivery = []
        for addr in configured:
            routed, _subject = redirect_email_address(addr)
            delivery.extend(routed or [addr])

    exclude = []
    assignee = getattr(ticket, "assigned_to", None) if ticket.assigned_to_id else None
    if assignee is not None and getattr(assignee, "email", None):
        exclude.append(assignee.email)
    return _dedupe(delivery, exclude=exclude)


def ticket_portal_link(ticket: Ticket) -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    return get_frontend_absolute_url(f"/admin-settings/support?ticket={ticket.ticket_id}")


def _ticket_alert_rows(ticket: Ticket) -> list[tuple[str, str]]:
    from iic_booking.communication.email_branding import format_email_datetime, user_department_name
    from iic_booking.communication.utils import booking_display_id_for_email

    user = ticket.user if ticket.user_id else None
    if user is not None:
        user_type = user.get_user_type_display_label() or ""
        department = user_department_name(user)
    else:
        user_type = PUBLIC_REQUESTER_LABEL
        department = ""

    rows: list[tuple[str, str]] = [
        ("Ticket", f"#{ticket.ticket_id}"),
        ("Subject", ticket.subject or ""),
        ("Category", str(ticket.get_ticket_type_display() or "")),
        ("Priority", str(ticket.get_priority_display() or "")),
        ("Raised by", ticket.get_user_name() or ""),
        ("Email", ticket.get_user_email() or ""),
        ("User type", user_type),
        ("Department", department),
        ("Phone", ticket.get_user_phone() or ""),
    ]
    if ticket.related_equipment_id:
        eq = ticket.related_equipment
        rows.append(("Equipment", " — ".join(p for p in (eq.code, eq.name) if p)))
    if ticket.related_booking_id:
        rows.append(("Booking", booking_display_id_for_email(ticket.related_booking)))
    if ticket.assigned_to_id:
        assignee = ticket.assigned_to
        rows.append(("Assigned to", f"{assignee.get_display_name()} ({assignee.email})"))
    if getattr(ticket, "attachment", None) and ticket.attachment.name:
        rows.append(("Attachment", ticket.attachment.name.rsplit("/", 1)[-1]))
    rows.append(("Raised at", format_email_datetime(ticket.created_at)))
    return [(label, value) for label, value in rows if value]


def build_ticket_alert_email(ticket: Ticket) -> tuple[str, str, str]:
    """(subject, plain text, branded HTML) for the new-ticket copy."""
    from iic_booking.communication.email_branding import (
        COLOR_PRIMARY,
        COLOR_SOFT_PANEL_BG,
        COLOR_SOFT_PANEL_BORDER,
        COLOR_TEXT,
        branded_plain_footer,
        cta_button_html,
        detail_row_html,
        details_card_html,
        paragraph_html,
        wrap_email_html,
    )

    rows = _ticket_alert_rows(ticket)
    link = ticket_portal_link(ticket)
    priority = str(ticket.get_priority_display() or "")
    subject_line = (ticket.subject or "").strip()
    if len(subject_line) > 120:
        subject_line = subject_line[:117] + "…"
    subject = f"New support ticket #{ticket.ticket_id} [{priority}]: {subject_line}"
    message = (ticket.description or "").strip() or "(No message)"

    text_lines = ["A new support ticket has been raised on the portal.", ""]
    text_lines += [f"{label}: {value}" for label, value in rows]
    text_lines += ["", "Message:", message]
    if link:
        text_lines += ["", f"Open ticket: {link}"]
    text = "\n".join(text_lines) + "\n" + branded_plain_footer()

    message_html = f"""
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" border="0" style="margin:16px 0 8px 0;">
  <tr>
    <td style="padding:16px 18px;background:{COLOR_SOFT_PANEL_BG};border:1px solid {COLOR_SOFT_PANEL_BORDER};border-radius:12px;">
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:{COLOR_PRIMARY};margin:0 0 10px 0;">Message</div>
      <div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;line-height:1.7;color:{COLOR_TEXT};white-space:pre-wrap;">{escape(message)}</div>
    </td>
  </tr>
</table>"""
    body = (
        paragraph_html("A new support ticket has been raised on the portal.")
        + details_card_html([detail_row_html(label, escape(value)) for label, value in rows], heading="Ticket details")
        + message_html
        + (cta_button_html(link, "Open ticket in portal") if link else "")
    )
    html = wrap_email_html(
        title=f"New support ticket #{ticket.ticket_id}",
        subtitle=f"{priority} priority · {ticket.get_ticket_type_display()}",
        body_inner_html=body,
        preheader=f"#{ticket.ticket_id}: {subject_line}",
    )
    return subject, text, html


def send_new_ticket_alert(ticket: Ticket) -> list[str]:
    """Send the copy now. Returns the addresses mailed; never raises."""
    try:
        recipients = alert_recipients_for(ticket)
        if not recipients:
            return []
        subject, text, html = build_ticket_alert_email(ticket)
        send_mail(
            subject=subject,
            message=text,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=recipients,
            html_message=html,
            fail_silently=False,
        )
        return recipients
    except Exception:
        logger.exception("New ticket alert email failed for ticket #%s", getattr(ticket, "ticket_id", None))
        return []


def _run_in_background(fn: Callable[[], None]) -> None:
    def _target() -> None:
        try:
            fn()
        finally:
            close_old_connections()

    threading.Thread(target=_target, name="support-ticket-alert", daemon=True).start()


def schedule_new_ticket_alert(ticket: Ticket) -> None:
    """Send the copy in the background once the ticket is committed, so ticket creation never waits or fails."""
    ticket_id = ticket.ticket_id

    def _send() -> None:
        try:
            fresh = Ticket.objects.select_related(
                "user", "user__department", "related_equipment", "related_booking", "related_booking__equipment", "assigned_to"
            ).get(pk=ticket_id)
        except Exception:
            logger.exception("New ticket alert: could not load ticket #%s", ticket_id)
            return
        send_new_ticket_alert(fresh)

    try:
        transaction.on_commit(lambda: _run_in_background(_send), robust=True)
    except Exception:
        logger.exception("Could not schedule new ticket alert for ticket #%s", ticket_id)
