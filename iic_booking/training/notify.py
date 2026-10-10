"""Email + in-app notifications for training workflow steps.

Templates are created from the branded catalog on first send when missing (never overwritten), so the
module works before ``sync_default_email_templates`` runs. Delivery is deferred to transaction commit and
failures never break the triggering action.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable

from django.db import transaction
from django.utils import timezone

from iic_booking.users.display import get_user_display_name

logger = logging.getLogger(__name__)


def ensure_template(code: str):
    from iic_booking.communication.default_email_templates import get_default_email_template
    from iic_booking.communication.models import CommunicationTemplate

    existing = CommunicationTemplate.objects.filter(
        code=code, communication_type=CommunicationTemplate.CommunicationType.EMAIL
    ).first()
    if existing is not None:
        return existing
    spec = get_default_email_template(code)
    if spec is None:
        return None
    fields = {k: spec[k] for k in ("name", "subject", "body_text", "body_html", "description", "variable_help")}
    template, _ = CommunicationTemplate.objects.get_or_create(
        code=code,
        communication_type=CommunicationTemplate.CommunicationType.EMAIL,
        defaults={**fields, "is_active": True},
    )
    return template


def frontend_link(path: str) -> str:
    from iic_booking.communication.utils import get_frontend_absolute_url

    return get_frontend_absolute_url(path) or path


def fmt_dt(value) -> str:
    if not value:
        return ""
    return timezone.localtime(value).strftime("%d %b %Y, %H:%M")


def fmt_window(start, end) -> str:
    if not start:
        return ""
    if not end:
        return fmt_dt(start)
    s, e = timezone.localtime(start), timezone.localtime(end)
    if s.date() == e.date():
        return f"{s.strftime('%d %b %Y, %H:%M')}–{e.strftime('%H:%M')}"
    return f"{fmt_dt(start)} – {fmt_dt(end)}"


def fmt_minutes(minutes) -> str:
    if not minutes:
        return ""
    h, m = divmod(int(minutes), 60)
    if h and m:
        return f"{h} h {m} min"
    return f"{h} h" if h else f"{m} min"


def send(
    code: str,
    recipients: Iterable[Any],
    *,
    context: dict[str, Any],
    title: str,
    message: str,
    path: str,
    actor=None,
    event: str = "",
    email_path: str = "",
) -> None:
    """``email_path`` overrides the email link only (e.g. a signed one-click link); in-app always uses ``path``."""
    from iic_booking.communication.in_app import _unique_active, notify_in_app

    users = _unique_active(recipients)
    if not users:
        return
    link = frontend_link(email_path or path)

    def _emails() -> None:
        from iic_booking.communication.service import CommunicationService

        try:
            template = ensure_template(code)
        except Exception:
            logger.exception("training template ensure failed code=%s", code)
            template = None
        if template is None:
            return
        for user in users:
            try:
                CommunicationService.send_email(
                    recipient=user,
                    template=template,
                    template_context={
                        **context,
                        "user_name": get_user_display_name(user),
                        "link": link,
                    },
                    created_by=actor,
                    metadata={"module": "training", "event": event or code},
                )
            except Exception:
                logger.exception("training email failed code=%s user_id=%s", code, user.id)

    try:
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(_emails)
        else:
            _emails()
    except Exception:
        logger.exception("training email dispatch failed code=%s", code)
    notify_in_app(users, title=title, message=message, link=path, event=event or f"training.{code}", created_by=actor)
