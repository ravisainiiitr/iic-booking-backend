"""
Show or set who receives a copy of every new support ticket (the Main Admin "New ticket email alerts" list).

Read-only unless --confirm ALERTS is given. Addresses are read from an environment variable named by
--set-from-env so they never appear in the command line, the repository or workflow logs; output is masked.

    manage.py support_ticket_alerts --json
    manage.py support_ticket_alerts --expect-from-env SUPPORT_TICKET_ALERT_EMAILS --dry-run-ticket latest --json
    SUPPORT_TICKET_ALERT_EMAILS=a@x.org,b@y.org \
        manage.py support_ticket_alerts --set-from-env SUPPORT_TICKET_ALERT_EMAILS --enabled on --confirm ALERTS
"""

from __future__ import annotations

import json
import os

from django.core.management.base import BaseCommand, CommandError

from iic_booking.support.email_lists import MAX_ALERT_RECIPIENTS, clean_email_list, mask_email
from iic_booking.support.models import SupportNotificationSettings, Ticket

CONFIRM_WORD = "ALERTS"


class Command(BaseCommand):
    help = "Show or set the recipients of the new support ticket copy (masked output)."

    def add_arguments(self, parser):
        parser.add_argument("--set-from-env", metavar="VAR", help="Replace the list with the addresses in env VAR.")
        parser.add_argument("--enabled", choices=["on", "off"], help="Switch the copy on or off.")
        parser.add_argument("--expect-from-env", metavar="VAR", help="Report whether the stored list equals env VAR.")
        parser.add_argument(
            "--dry-run-ticket",
            metavar="ID|latest",
            help="Show who would get the copy for an existing ticket. Sends nothing.",
        )
        parser.add_argument("--confirm", default="", help=f"Type {CONFIRM_WORD} to write changes.")
        parser.add_argument("--json", action="store_true", help="Print a JSON report.")

    def handle(self, *args, **opts):
        cfg = SupportNotificationSettings.get_singleton()
        changes: list[str] = []

        if opts["set_from_env"] or opts["enabled"]:
            if opts["confirm"] != CONFIRM_WORD:
                raise CommandError(f"Pass --confirm {CONFIRM_WORD} to change the settings.")
            enabled = cfg.ticket_alert_enabled if not opts["enabled"] else opts["enabled"] == "on"
            emails, _invalid = clean_email_list(cfg.ticket_alert_emails)
            if opts["set_from_env"]:
                emails = self._emails_from_env(opts["set_from_env"])
            if enabled and not emails:
                raise CommandError("Refusing to switch the copy on with no recipients.")
            if enabled != cfg.ticket_alert_enabled:
                changes.append(f"enabled -> {'on' if enabled else 'off'}")
            stored = ", ".join(emails)
            if stored != cfg.ticket_alert_emails:
                changes.append(f"recipients -> {len(emails)} address(es)")
            if changes:
                cfg.ticket_alert_enabled = enabled
                cfg.ticket_alert_emails = stored
                cfg.full_clean()
                cfg.save()

        report = {"changes": changes, "state": self._state(cfg)}
        if opts["expect_from_env"]:
            expected = self._emails_from_env(opts["expect_from_env"])
            current, _invalid = clean_email_list(cfg.ticket_alert_emails)
            report["matches_expected"] = sorted(e.lower() for e in current) == sorted(e.lower() for e in expected)
        if opts["dry_run_ticket"]:
            report["dry_run"] = self._dry_run(opts["dry_run_ticket"])

        if opts["json"]:
            self.stdout.write(json.dumps(report, indent=2))
            return
        st = report["state"]
        self.stdout.write(
            f"New ticket copy: {'ON' if st['enabled'] else 'OFF'}; "
            f"{st['recipient_count']} recipient(s): {', '.join(st['recipients_masked']) or 'none'}"
        )
        for change in changes:
            self.stdout.write(f"changed: {change}")
        if "matches_expected" in report:
            self.stdout.write(f"matches expected: {report['matches_expected']}")
        if "dry_run" in report:
            self.stdout.write(f"dry run: {json.dumps(report['dry_run'])}")

    def _emails_from_env(self, var: str) -> list[str]:
        raw = os.environ.get(var, "")
        emails, invalid = clean_email_list(raw)
        if invalid:
            raise CommandError(f"{len(invalid)} invalid address(es) in ${var}.")
        if not emails:
            raise CommandError(f"${var} is empty.")
        if len(emails) > MAX_ALERT_RECIPIENTS:
            raise CommandError(f"At most {MAX_ALERT_RECIPIENTS} recipients are allowed.")
        return emails

    @staticmethod
    def _state(cfg: SupportNotificationSettings) -> dict:
        valid, invalid = clean_email_list(cfg.ticket_alert_emails)
        return {
            "enabled": bool(cfg.ticket_alert_enabled),
            "recipient_count": len(valid),
            "recipients_masked": [mask_email(e) for e in valid],
            "invalid_count": len(invalid),
            "updated_at": cfg.updated_at.isoformat() if cfg.updated_at else None,
        }

    @staticmethod
    def _dry_run(which: str) -> dict:
        from iic_booking.support.ticket_alerts import alert_recipients_for, build_ticket_alert_email

        qs = Ticket.objects.select_related("user", "related_equipment", "related_booking", "assigned_to")
        if which == "latest":
            ticket = qs.order_by("-ticket_id").first()
        elif which.isdigit():
            ticket = qs.filter(pk=int(which)).first()
        else:
            raise CommandError("--dry-run-ticket takes a ticket number or 'latest'.")
        if ticket is None:
            return {"ticket_id": None, "would_send": 0, "note": "no such ticket"}
        recipients = alert_recipients_for(ticket)
        subject, _text, _html = build_ticket_alert_email(ticket)
        return {
            "ticket_id": ticket.ticket_id,
            "would_send": len(recipients),
            "recipients_masked": [mask_email(e) for e in recipients],
            "one_message_per_recipient": True,
            "subject_starts_with": subject.split(":", 1)[0],
        }
