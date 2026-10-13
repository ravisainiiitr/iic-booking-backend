"""Background delivery for held emails (daily digest / quiet hours) and browser push."""

from __future__ import annotations

import logging
from itertools import groupby

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.utils import timezone
from django.utils.html import escape, linebreaks

from .models import CommunicationLog, NotificationDigestItem

logger = logging.getLogger(__name__)

EXCERPT_CHARS = 400


def _excerpt(text: str) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= EXCERPT_CHARS else text[: EXCERPT_CHARS - 1].rstrip() + "…"


def _absolute(link: str) -> str:
    from .email_branding import absolute_http_url
    from .utils import get_frontend_absolute_url

    if not link:
        return ""
    return absolute_http_url(link) or (get_frontend_absolute_url(link) if link.startswith("/") else "")


def build_digest(user, items) -> tuple[str, str, str]:
    """(subject, text, html) for one person's held emails, newest last."""
    from . import notification_preferences as prefs
    from .email_branding import cta_button_html, portal_url, user_display_name, wrap_email_html

    count = len(items)
    reasons = {item.reason for item in items}
    if count == 1:
        subject = items[0].log.subject
    elif reasons == {NotificationDigestItem.Reason.QUIET}:
        subject = f"{count} updates from the IIC portal while you were away"
    else:
        subject = f"Your daily IIC portal digest: {count} updates"

    settings_url = f"{portal_url().rstrip('/')}/notifications?tab=settings"
    text_parts = [f"Dear {user_display_name(user)},", ""]
    html_rows = []
    for item in items:
        label = prefs.BY_KEY[item.category].label if item.category in prefs.BY_KEY else "Update"
        when = timezone.localtime(item.created_at).strftime("%d %b, %H:%M")
        link = _absolute(item.link)
        excerpt = _excerpt(item.log.message)
        text_parts += [f"* {item.log.subject} ({label}, {when})", f"  {excerpt}"]
        if link:
            text_parts.append(f"  {link}")
        text_parts.append("")
        open_link = (
            f'<a href="{escape(link)}" style="color:#1d6fa3;text-decoration:none;font-weight:600;">Open</a>'
            if link
            else ""
        )
        html_rows.append(
            '<tr><td style="padding:14px 0;border-bottom:1px solid #e2e8f0;font-family:Arial,Helvetica,sans-serif;">'
            f'<div style="font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:#64748b;">'
            f"{escape(label)} · {escape(when)}</div>"
            f'<div style="font-size:15px;font-weight:600;color:#0f172a;margin-top:4px;">{escape(item.log.subject)}</div>'
            f'<div style="font-size:13px;line-height:1.55;color:#334155;margin-top:4px;">{linebreaks(escape(excerpt))}</div>'
            f"{open_link}</td></tr>"
        )
    text_parts += [f"Change how and when you get these emails: {settings_url}"]
    body = (
        f'<p style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#0f172a;">'
        f"Dear {escape(user_display_name(user))},</p>"
        f'<table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(html_rows)}</table>'
        f'<div style="margin-top:20px;">{cta_button_html(settings_url, "Notification settings", primary=False)}</div>'
    )
    html = wrap_email_html(
        title="Your portal updates" if count > 1 else items[0].log.subject,
        subtitle=f"{count} update{'s' if count != 1 else ''}",
        body_inner_html=body,
        preheader=items[-1].log.subject,
    )
    return subject, "\n".join(text_parts), html


def _send_digest_for_user(user_id: int, now) -> int:
    from iic_booking.users.test_accounts import redirect_email_for_user

    with transaction.atomic():
        items = list(
            NotificationDigestItem.objects.select_for_update(skip_locked=True, of=("self",))
            .filter(user_id=user_id, sent_at__isnull=True, deliver_after__lte=now)
            .select_related("log", "user")
            .order_by("created_at")
        )
        if not items:
            return 0
        user = items[0].user
        held_logs = [item.log for item in items]
        if not user.is_active or not (user.email or "").strip():
            NotificationDigestItem.objects.filter(pk__in=[i.pk for i in items]).update(sent_at=now)
            CommunicationLog.objects.filter(pk__in=[log.pk for log in held_logs]).update(
                status=CommunicationLog.CommunicationStatus.REJECTED, error_message="Recipient inactive"
            )
            return 0
        subject, text, html = build_digest(user, items)
        delivery_emails, subject = redirect_email_for_user(user, original_email=user.email, subject=subject)
        delivery_emails = delivery_emails or [user.email]
        msg = EmailMultiAlternatives(
            subject=subject, body=text, from_email=settings.DEFAULT_FROM_EMAIL, to=delivery_emails
        )
        msg.attach_alternative(html, "text/html")
        msg.send(fail_silently=False)
        digest_log = CommunicationLog.objects.create(
            communication_type=CommunicationLog.CommunicationType.EMAIL,
            recipient=user,
            recipient_email=", ".join(delivery_emails)[:255],
            subject=subject[:255],
            message=text,
            status=CommunicationLog.CommunicationStatus.SENT,
            sent_at=timezone.now(),
            metadata={"digest_of": [log.pk for log in held_logs], "notification_category": "digest"},
        )
        for log in held_logs:
            log.status = CommunicationLog.CommunicationStatus.SENT
            log.sent_at = digest_log.sent_at
            log.metadata = {**(log.metadata or {}), "delivered_in": digest_log.pk}
            log.save(update_fields=["status", "sent_at", "metadata", "updated_at"])
        NotificationDigestItem.objects.filter(pk__in=[i.pk for i in items]).update(sent_at=digest_log.sent_at)
        return len(items)


@shared_task(name="communication.send_due_notification_digests")
def send_due_notification_digests() -> int:
    """Email every person whose held notifications are due, as one message each."""
    now = timezone.now()
    user_ids = (
        NotificationDigestItem.objects.filter(sent_at__isnull=True, deliver_after__lte=now)
        .order_by("user_id")
        .values_list("user_id", flat=True)
    )
    delivered = 0
    for user_id, _ in groupby(user_ids):
        try:
            delivered += _send_digest_for_user(user_id, now)
        except Exception:
            logger.exception("Digest email failed for user %s; will retry next run", user_id)
    if delivered:
        logger.info("Delivered %s held notifications in digests", delivered)
    return delivered


@shared_task(name="communication.deliver_web_push", ignore_result=True)
def deliver_web_push(log_id: int) -> int:
    from .web_push import deliver_for_log

    log = CommunicationLog.objects.select_related("recipient").filter(pk=log_id).first()
    if log is None:
        return 0
    return deliver_for_log(log)
