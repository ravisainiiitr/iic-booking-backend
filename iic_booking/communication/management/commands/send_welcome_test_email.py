"""
Send a styled welcome email to a recipient for testing.

Example:
  py -3 manage.py send_welcome_test_email ravis.mic2014@iitr.ac.in
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from iic_booking.communication.welcome_email import build_welcome_email


class Command(BaseCommand):
    help = "Send a styled welcome email to a recipient for testing."

    def add_arguments(self, parser):
        parser.add_argument("recipient", help="Recipient email address")
        parser.add_argument("--name", default=None, help="Optional display name to personalize the email")
        parser.add_argument(
            "--user-type",
            default=None,
            help="Optional user_type for conditional text (student/faculty/etc.)",
        )

    def handle(self, *args, **options):
        recipient = (options.get("recipient") or "").strip()
        if not recipient:
            self.stderr.write(self.style.ERROR("Recipient email address is required."))
            raise SystemExit(1)

        recipient_name = options.get("name")
        user_type = options.get("user_type")

        content = build_welcome_email(
            recipient_name=recipient_name,
            recipient_email=recipient,
            user_type=user_type,
        )

        send_mail(
            subject=content.subject,
            message=content.text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[recipient],
            html_message=content.html_body,
            fail_silently=False,
        )

        self.stdout.write(self.style.SUCCESS(f"Welcome test email sent to {recipient}."))

