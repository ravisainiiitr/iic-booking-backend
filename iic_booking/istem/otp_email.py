"""Branded I-STEM OTP emails. They are rendered from the communication templates but sent directly, so the code never reaches
``CommunicationLog``, which keeps the text of what it sends. If rendering fails the plain wording still goes out: the OTP matters
more than its styling."""

from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

PORTAL_NAME = "IIT Roorkee Equipment Booking Portal"


def _rendered(code: str, context: dict) -> dict:
    from iic_booking.communication.default_email_templates import get_default_email_template
    from iic_booking.communication.models import CommunicationTemplate
    from iic_booking.communication.service import CommunicationService

    template = CommunicationTemplate.objects.filter(code=code, communication_type="email", is_active=True).first()
    if template is None:  # code deployed, migration not run yet
        spec = get_default_email_template(code)
        template = CommunicationTemplate(code=code, communication_type="email", subject=spec["subject"], body_text=spec["body_text"], body_html=spec["body_html"])
    return CommunicationService.render_template(template, context=context)


def send_otp_email(template_code: str, *, recipients: list[str], context: dict, fallback_subject: str, fallback_body: str, subject_wrap=None) -> None:
    """Raises only when the mail itself cannot be sent, as the callers expect."""
    subject, body, markup = fallback_subject, fallback_body, None
    try:
        rendered = _rendered(template_code, {"portal_name": PORTAL_NAME, **context})
        if rendered.get("subject") and rendered.get("message"):
            subject, body, markup = rendered["subject"], rendered["message"], rendered.get("html_message")
    except Exception:  # noqa: BLE001 - falls back to plain wording; the exception text could quote the code, so it is not logged
        logger.warning("I-STEM OTP email template %s could not be rendered; sending plain wording", template_code)
    if subject_wrap:
        subject = subject_wrap(subject)
    send_mail(subject=subject, message=body, from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=recipients, html_message=markup, fail_silently=False)
