"""
Send a styled welcome email to a recipient for testing.

Example:
  py -3 manage.py send_welcome_test_email someone@iitr.ac.in --name "Ada" --user-type faculty --department "Physics"
"""

from __future__ import annotations

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand

from iic_booking.communication.welcome_email import build_welcome_email


class Command(BaseCommand):
    help = "Send a styled first-login welcome email to a recipient for testing."

    def add_arguments(self, parser):
        parser.add_argument("recipient", help="Recipient email address")
        parser.add_argument("--name", default=None, help="Optional display name")
        parser.add_argument(
            "--user-type",
            default="faculty",
            help="user_type for role-specific copy (student/faculty/dept_admin/…)",
        )
        parser.add_argument(
            "--user-type-alias",
            default=None,
            help="Optional display alias (e.g. IITR Post Doctoral Fellows)",
        )
        parser.add_argument(
            "--department",
            default=None,
            help="Optional department name for personalization",
        )

    def handle(self, *args, **options):
        recipient = (options.get("recipient") or "").strip()
        if not recipient:
            self.stderr.write(self.style.ERROR("Recipient email address is required."))
            raise SystemExit(1)

        content = build_welcome_email(
            recipient_name=options.get("name"),
            recipient_email=recipient,
            user_type=options.get("user_type"),
            user_type_alias=options.get("user_type_alias"),
            department_name=options.get("department"),
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
        self.stdout.write(f"Subject: {content.subject}")
