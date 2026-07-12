"""
Patch stored CommunicationTemplate rows for booking emails: standardize footers so
plain text uses "Note: {{ comment }}" where appropriate, and HTML wraps {{ link }} in
<a href="..."> when it was stored as plain text.

Safe to run multiple times (idempotent replacements).

Usage:
  python manage.py patch_booking_email_template_footers --dry-run
  python manage.py patch_booking_email_template_footers
  python manage.py patch_booking_email_template_footers --code booking_completed_email
"""

from typing import List, Tuple

from django.core.management.base import BaseCommand

from iic_booking.communication.models import CommunicationTemplate

DEFAULT_CODES_PREFIX = "booking_"

# Do not add "Note:" before comment for templates that use a "Comment:" block
EXCLUDE_FROM_NOTE_PREFIX = frozenset({"booking_comment_email"})

# Plain text: idempotent pairs (old, new). Order matters: longer/more specific first.
TEXT_REPLACEMENTS: List[Tuple[str, str]] = [
    (
        "\n\n{{ comment }}\n\nYou can view your booking details at:",
        "\n\nNote: {{ comment }}\n\nYou can view your booking details at:",
    ),
    (
        "\n{{ comment }}\n\nYou can view your booking details at:",
        "\nNote: {{ comment }}\n\nYou can view your booking details at:",
    ),
    (
        "\n\n{{ comment }}\n\nThe refund amount has been credited to your wallet.",
        "\n\nNote: {{ comment }}\n\nThe refund amount has been credited to your wallet.",
    ),
    (
        "\n{{ comment }}\n\nThe refund amount has been credited to your wallet.",
        "\nNote: {{ comment }}\n\nThe refund amount has been credited to your wallet.",
    ),
    (
        "\n\n{{ comment }}\n\nIf you have any questions",
        "\n\nNote: {{ comment }}\n\nIf you have any questions",
    ),
]

# HTML: only applied when href="{{ link }}" is absent (see _patch_body_html)
HTML_LINK_REPLACEMENTS: List[Tuple[str, str]] = [
    (
        "You can view your booking details at: {{ link }}",
        'You can view your booking details at: <a href="{{ link }}">{{ link }}</a>',
    ),
    (
        "You can view your booking details at: {{ link }}.",
        'You can view your booking details at: <a href="{{ link }}">{{ link }}</a>.',
    ),
    (
        "You can view your booking and wallet balance here: {{ link }}",
        'You can view your booking and wallet balance here: <a href="{{ link }}">{{ link }}</a>',
    ),
    (
        "You can view your booking and wallet balance here: {{ link }}.",
        'You can view your booking and wallet balance here: <a href="{{ link }}">{{ link }}</a>.',
    ),
    (
        "The refund amount has been credited to your wallet. You can view your booking details at: {{ link }}",
        'The refund amount has been credited to your wallet. You can view your booking details at: <a href="{{ link }}">{{ link }}</a>',
    ),
    (
        "The refund amount has been credited to your wallet. You can view your booking details at: {{ link }}.",
        'The refund amount has been credited to your wallet. You can view your booking details at: <a href="{{ link }}">{{ link }}</a>.',
    ),
]

VARIABLE_HELP_SENTINEL = "{{ comment }} is empty when there is no note"


def _patch_body_text(code: str, body: str) -> Tuple[str, bool]:
    if not body or code in EXCLUDE_FROM_NOTE_PREFIX:
        return body, False
    out = body
    changed = False
    for old, new in TEXT_REPLACEMENTS:
        if old in out:
            out = out.replace(old, new)
            changed = True
    return out, changed


def _patch_body_html(body: str) -> Tuple[str, bool]:
    if not body or "{{ link }}" not in body:
        return body, False
    if 'href="{{ link }}"' in body or "href='{{ link }}'" in body:
        return body, False
    out = body
    changed = False
    for old, new in HTML_LINK_REPLACEMENTS:
        if old in out:
            out = out.replace(old, new)
            changed = True
    return out, changed


def _patch_variable_help(help_text: str) -> Tuple[str, bool]:
    if not help_text or VARIABLE_HELP_SENTINEL in help_text:
        return help_text, False
    if "{{ comment }}" not in help_text and "{{ link }}" not in help_text:
        return help_text, False
    append = (
        " " + VARIABLE_HELP_SENTINEL + "; "
        "{{ link }} is the My Bookings URL when FRONTEND_URL is configured."
    )
    return help_text + append, True


class Command(BaseCommand):
    help = (
        "Patch booking_* email templates in DB: Note/comment in plain text and "
        "<a href> for {{ link }} in HTML when missing."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would change; do not save.",
        )
        parser.add_argument(
            "--code",
            action="append",
            dest="codes",
            default=None,
            help="Limit to this template code (repeatable). Default: code starting with booking_",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        codes = options["codes"]

        qs = CommunicationTemplate.objects.filter(
            communication_type=CommunicationTemplate.CommunicationType.EMAIL,
        )
        if codes:
            qs = qs.filter(code__in=codes)
        else:
            qs = qs.filter(code__startswith=DEFAULT_CODES_PREFIX)

        total = qs.count()
        self.stdout.write(f"Scanning {total} email template(s)...")

        updated = 0
        for t in qs.order_by("code"):
            new_text, c1 = _patch_body_text(t.code, t.body_text or "")
            new_html, c2 = _patch_body_html(t.body_html or "")
            new_help, c3 = _patch_variable_help(t.variable_help or "")

            changes = []
            if c1:
                changes.append("body_text")
            if c2:
                changes.append("body_html")
            if c3:
                changes.append("variable_help")

            if not changes:
                continue

            self.stdout.write(f"  [{t.code}] " + ", ".join(changes))
            updated += 1

            if dry_run:
                continue

            t.body_text = new_text
            t.body_html = new_html
            t.variable_help = new_help
            t.save()

        if dry_run:
            self.stdout.write(
                self.style.WARNING(
                    f"Dry run: would update {updated} template(s). "
                    "Run without --dry-run to apply."
                )
            )
        else:
            self.stdout.write(self.style.SUCCESS(f"Updated {updated} template(s)."))
