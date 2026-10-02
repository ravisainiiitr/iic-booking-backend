"""One email per user whose saved booking templates would fail, or are likely to fail, when booking opens.

Usage:
  python manage.py notify_template_health                 # dry run: masked recipients and counts only
  python manage.py notify_template_health --send          # send the emails
  python manage.py notify_template_health --opens-at "2026-10-07 21:00"

Issues are worked out as of the booking opening (so quota is for the week that opens then). Each email is
recorded in CommunicationLog with the template/issue pairs it covered; a pair already emailed is not sent
again, so re-running only mails new issues.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from django.utils.html import escape

from iic_booking.communication.email_branding import COLOR_PRIMARY, user_display_name, wrap_email_html
from iic_booking.communication.models import CommunicationLog
from iic_booking.communication.utils import get_frontend_absolute_url
from iic_booking.equipment.models import BookingInputTemplate
from iic_booking.equipment.template_health import ERROR, UNFIXABLE_CODES, check_template

NOTICE_KEY = "template_health_notice"
LIKELY_FAIL_CODES = frozenset({"quota_low", "wallet_low", "no_wallet"})
WALLET_CODES = frozenset({"wallet_low", "no_wallet"})
OPENING_WEEKDAY = 2  # Wednesday
OPENING_TIME = time(21, 0)
QUIET_LOGGERS = ("iic_booking.communication", "django.core.mail", "django_ses", "botocore", "boto3")
NOT_SENT = (CommunicationLog.CommunicationStatus.FAILED,)


def mask_email(email: str) -> str:
    local, _, domain = (email or "").partition("@")
    if not local or not domain:
        return "***"
    return f"{local[0]}***@{domain}"


def next_opening(now=None) -> datetime:
    """The next Wednesday 21:00 (local time) at or after ``now``."""
    local = timezone.localtime(now or timezone.now())
    days = (OPENING_WEEKDAY - local.weekday()) % 7
    opening = timezone.make_aware(datetime.combine(local.date() + timedelta(days=days), OPENING_TIME))
    return opening if opening >= local else opening + timedelta(days=7)


def notice_issues(health) -> list[dict]:
    """Issues that stop the template booking as saved (and can be fixed by its owner), or likely will."""
    return [
        i for i in health.get("issues") or []
        if (i.get("severity") == ERROR and i.get("code") not in UNFIXABLE_CODES) or i.get("code") in LIKELY_FAIL_CODES
    ]


def fix_link(template, field=None) -> str:
    params = {"equipment_id": template.equipment_id, "mode": "template", "template_id": template.pk,
              "return_to": "/booking-templates"}
    if field:
        params["fix"] = field
    return get_frontend_absolute_url(f"/book-equipment?{urlencode(params)}")


def _rupees(value) -> str:
    try:
        return f"₹{Decimal(str(value)):,.0f}"
    except (InvalidOperation, ValueError):
        return ""


def issue_text(issue) -> str:
    code = issue.get("code")
    if code == "wallet_low":
        amount = _rupees(issue.get("charge"))
        charge = f" of {amount}" if amount else ""
        return f"Your wallet balance may not cover the estimated charge{charge}. Please recharge, or ask your supervisor."
    if code == "no_wallet":
        return (
            "You are not linked to a wallet yet, so this booking cannot be paid for. Ask your supervisor to add you "
            "to their wallet, or send them a join request from the Wallet page."
        )
    if code == "quota_low":
        return f"{issue.get('message', '').strip()} Reduce the samples, or book the rest in a later week."
    if code in ("numeric_min", "numeric_max", "numeric_formula_max"):
        return f"{issue.get('message', '').strip()} Change it and save the template."
    return (issue.get("message") or "").strip()


def build_email(user, items, opens_at):
    """Subject, plain text and HTML for ``items``: [(template, [issue, ...]), ...]."""
    when = timezone.localtime(opens_at)
    week = (when.date() + timedelta(days=7 - when.weekday())).strftime("%d %B").lstrip("0")
    opens = f"{when:%A}, {when.day} {when:%B} at {when.strftime('%I:%M %p').lstrip('0').lower()}"
    many = len(items) > 1
    noun = "templates" if many else "template"
    subject = f"Please check your booking {noun} before booking opens on {when:%A} {when.strftime('%I %p').lstrip('0').lower()}"
    name = user_display_name(user, fallback="there")
    intro = (
        f"Booking for the week of {week} opens on {opens}. We checked your saved booking {noun} and found "
        f"something that may stop {'them' if many else 'it'} from booking as saved."
    )
    wallet_link = get_frontend_absolute_url("/wallet")

    text = [f"Dear {name},", "", intro, ""]
    blocks = []
    for template, issues in items:
        equipment = getattr(template.equipment, "name", "") or "the equipment"
        field = next((i.get("field") for i in issues if i.get("field") and i.get("code") not in WALLET_CODES), None)
        link = fix_link(template, field) if any(i.get("code") not in WALLET_CODES for i in issues) else ""
        lines = [issue_text(i) for i in issues]
        text.append(f'Template "{template.name}" for {equipment}:')
        text.extend(f"  - {line}" for line in lines)
        if link:
            text.append(f"  Open the template: {link}")
        if wallet_link and any(i.get("code") in WALLET_CODES for i in issues):
            text.append(f"  Wallet: {wallet_link}")
        text.append("")

        buttons = []
        if link:
            buttons.append(
                f"<a href='{escape(link)}' style='display:inline-block;background:{COLOR_PRIMARY};color:#fff;"
                "padding:9px 14px;border-radius:8px;text-decoration:none;font-weight:700;margin-right:8px;'>"
                "Open the template</a>"
            )
        if wallet_link and any(i.get("code") in WALLET_CODES for i in issues):
            buttons.append(
                f"<a href='{escape(wallet_link)}' style='display:inline-block;color:{COLOR_PRIMARY};padding:9px 4px;"
                "text-decoration:none;font-weight:700;'>Open your wallet</a>"
            )
        blocks.append(
            "<div style='border:1px solid #e2e8f0;border-radius:10px;padding:14px 16px;margin:0 0 14px 0;'>"
            f"<p style='margin:0 0 8px 0;'><b>{escape(template.name)}</b> &middot; {escape(equipment)}</p>"
            "<ul style='margin:0 0 10px 18px;padding:0;'>"
            + "".join(f"<li style='margin:0 0 6px 0;'>{escape(line)}</li>" for line in lines)
            + "</ul>"
            + (f"<p style='margin:0;'>{''.join(buttons)}</p>" if buttons else "")
            + "</div>"
        )
    codes = {i.get("code") for _t, issues in items for i in issues}
    steps = []
    if codes - WALLET_CODES:
        steps.append("open the template, correct what is listed above and save it")
    if codes & WALLET_CODES:
        steps.append("sort out the wallet")
    outro = (
        f"Please {' and '.join(steps)} before booking opens. If you no longer need {'these' if many else 'this'} "
        f"{noun}, you can ignore this email. You will not get this email again for the same {noun}."
    )
    text.extend([outro, "", "Thank you."])
    body = (
        f"<p style='margin:0 0 12px 0;'>Dear {escape(name)},</p>"
        f"<p style='margin:0 0 16px 0;'>{escape(intro)}</p>"
        + "".join(blocks)
        + f"<p style='margin:4px 0 0 0;'>{escape(outro)}</p>"
    )
    html = wrap_email_html(
        title="Check your booking templates" if many else "Check your booking template",
        subtitle=f"Booking opens {opens}",
        body_inner_html=body,
        preheader=intro,
    )
    return subject, "\n".join(text), html


def already_notified_keys(user) -> set[str]:
    keys = set()
    logs = CommunicationLog.objects.filter(
        recipient=user, communication_type=CommunicationLog.CommunicationType.EMAIL, metadata__has_key=NOTICE_KEY,
    ).exclude(status__in=NOT_SENT)
    for metadata in logs.values_list("metadata", flat=True):
        keys.update((metadata or {}).get(NOTICE_KEY, {}).get("keys") or [])
    return keys


class Command(BaseCommand):
    help = "Email users whose booking templates would fail, or are likely to fail, when booking opens (dry run by default)."

    def add_arguments(self, parser):
        parser.add_argument("--send", action="store_true", help="Send the emails (without it nothing is sent).")
        parser.add_argument("--dry-run", action="store_true", help="Only list masked recipients and counts (default).")
        parser.add_argument(
            "--opens-at", default=None,
            help='Local date and time booking opens, e.g. "2026-10-07 21:00" (default: next Wednesday 21:00).',
        )
        parser.add_argument(
            "--max-recipients", type=int, default=10,
            help="Do not send if more users than this would be emailed (default 10).",
        )

    def handle(self, *args, **options):
        if options["send"] and options["dry_run"]:
            raise CommandError("Use either --send or --dry-run.")
        send = bool(options["send"])
        opens_at = self._opens_at(options["opens_at"])
        for name in QUIET_LOGGERS:
            logging.getLogger(name).setLevel(logging.WARNING)

        templates = [
            t for t in BookingInputTemplate.objects.select_related("user", "equipment").filter(equipment__isnull=False).order_by("user_id", "pk")
            if t.user.is_active
        ]
        found = defaultdict(list)
        codes = Counter()
        for template in templates:
            issues = notice_issues(check_template(template, use_cache=False, now=opens_at))
            if issues:
                found[template.user].append((template, issues))
                codes.update({i["code"] for i in issues})

        week = timezone.localtime(opens_at).date() + timedelta(days=7 - timezone.localtime(opens_at).weekday())
        out = self.stdout
        out.write(f"Booking opens {timezone.localtime(opens_at):%Y-%m-%d %H:%M %Z}; quota checked for the week of {week}")
        out.write(f"Templates checked: {len(templates)} (users: {len({t.user_id for t in templates})})")
        out.write(
            f"Templates needing a notice: {sum(len(v) for v in found.values())}; users: {len(found)}"
        )
        for code, count in sorted(codes.items()):
            out.write(f"  {code}: {count}")

        recipients = []
        skipped = 0
        for user, items in found.items():
            sent_keys = already_notified_keys(user)
            fresh = []
            for template, issues in items:
                new = [i for i in issues if f"{template.pk}:{i['code']}" not in sent_keys]
                skipped += len(issues) - len(new)
                if new:
                    fresh.append((template, new))
            if fresh:
                recipients.append((user, fresh))
        if skipped:
            out.write(f"Already emailed (skipped): {skipped} issue(s)")
        no_email = [u for u, _ in recipients if not (u.email or "").strip()]
        recipients = [(u, items) for u, items in recipients if (u.email or "").strip()]
        if no_email:
            out.write(f"Users without an email address (skipped): {len(no_email)}")

        out.write(f"Recipients: {len(recipients)}")
        for user, items in recipients:
            issue_codes = sorted({i["code"] for _t, issues in items for i in issues})
            out.write(
                f"  {mask_email(user.email)}  templates={','.join(str(t.pk) for t, _ in items)}  issues={','.join(issue_codes)}"
            )

        if not send:
            out.write("DRY RUN: no email sent. Re-run with --send to send.")
            return
        if len(recipients) > options["max_recipients"]:
            raise CommandError(
                f"{len(recipients)} recipients is more than --max-recipients={options['max_recipients']}; nothing sent."
            )

        sent = failed = 0
        for user, items in recipients:
            if self._send(user, items, opens_at):
                sent += 1
            else:
                failed += 1
        out.write(f"SENT: {sent} email(s); failed: {failed}")
        if failed:
            raise CommandError(f"{failed} email(s) failed; re-run to retry them.")

    def _opens_at(self, raw):
        if not raw:
            return next_opening()
        try:
            parsed = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M")
        except ValueError as exc:
            raise CommandError('--opens-at must look like "2026-10-07 21:00".') from exc
        return timezone.make_aware(parsed)

    def _send(self, user, items, opens_at) -> bool:
        from iic_booking.users.test_accounts import redirect_email_address

        subject, text, html = build_email(user, items, opens_at)
        delivery, subject = redirect_email_address(user.email, subject=subject)
        delivery = delivery or [user.email]
        metadata = {
            NOTICE_KEY: {
                "keys": [f"{t.pk}:{i['code']}" for t, issues in items for i in issues],
                "template_ids": [t.pk for t, _ in items],
                "opens_at": opens_at.isoformat(),
            },
            "email_backend": getattr(settings, "EMAIL_BACKEND", ""),
        }
        if [e.lower() for e in delivery] != [user.email.lower()]:
            metadata["test_account_email_redirect"] = ", ".join(delivery)
        log = CommunicationLog.objects.create(
            communication_type=CommunicationLog.CommunicationType.EMAIL,
            recipient=user,
            recipient_email=", ".join(delivery)[:255],
            subject=subject[:255],
            message=text,
            status=CommunicationLog.CommunicationStatus.PENDING,
            metadata=metadata,
        )
        try:
            send_mail(
                subject=subject, message=text, from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=delivery, html_message=html, fail_silently=False,
            )
        except Exception as exc:  # noqa: BLE001
            log.status = CommunicationLog.CommunicationStatus.FAILED
            log.error_message = str(exc)
            log.save(update_fields=["status", "error_message", "updated_at"])
            self.stdout.write(f"  FAILED {mask_email(user.email)}: {type(exc).__name__}")
            return False
        log.status = CommunicationLog.CommunicationStatus.SENT
        log.sent_at = timezone.now()
        log.save(update_fields=["status", "sent_at", "updated_at"])
        self.stdout.write(f"  sent {mask_email(user.email)}")
        return True
