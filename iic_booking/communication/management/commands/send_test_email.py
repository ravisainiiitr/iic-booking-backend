"""Management command to test outgoing email (SMTP/SES)."""

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Send a test email to verify SMTP/SES configuration. Usage: python manage.py send_test_email recipient@example.com"

    def add_arguments(self, parser):
        parser.add_argument(
            "recipient",
            nargs="?",
            default=None,
            help="Recipient email address (required unless --config-only)",
        )
        parser.add_argument(
            "--config-only",
            action="store_true",
            help="Only print current email backend and host (no send).",
        )

    def handle(self, *args, **options):
        if options["config_only"]:
            self.stdout.write(
                self.style.WARNING(
                    f"Backend: {settings.EMAIL_BACKEND}\n"
                    f"USE_AWS_SES_API: {getattr(settings, 'USE_AWS_SES_API', False)}\n"
                    f"Host: {getattr(settings, 'EMAIL_HOST', 'N/A')}\n"
                    f"Port: {getattr(settings, 'EMAIL_PORT', 'N/A')}\n"
                    f"Use TLS: {getattr(settings, 'EMAIL_USE_TLS', 'N/A')}\n"
                    f"User: {'(set)' if getattr(settings, 'EMAIL_HOST_USER', '') else '(empty — email will fail)'}\n"
                    f"Password: {'(set)' if getattr(settings, 'EMAIL_HOST_PASSWORD', '') else '(empty — email will fail)'}\n"
                    f"From: {settings.DEFAULT_FROM_EMAIL}"
                )
            )
            return

        recipient = options.get("recipient") or ""
        recipient = recipient.strip()
        if not recipient:
            self.stderr.write(
                self.style.ERROR("Provide a recipient email: python manage.py send_test_email recipient@example.com")
            )
            raise SystemExit(1)

        subject = "[Institute Equipment Booking Portal] Test email"
        message = "This is a test email from the Institute Equipment Booking Portal. If you received this, outgoing email is working."
        from_email = settings.DEFAULT_FROM_EMAIL

        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=[recipient],
                fail_silently=False,
            )
            self.stdout.write(self.style.SUCCESS(f"Test email sent to {recipient}."))
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"Failed to send test email: {e}"))
            import traceback
            traceback.print_exc()
            raise SystemExit(1)
